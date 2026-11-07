"""音樂：用關鍵字或網址搜尋並播放（Lavalink + Wavelink）。

音樂節點（Lavalink v4 伺服器）請在 config.LAVALINK_NODES 自訂，例如 node01.example.com。
語音連線、/join、/leave、/247 由 voice.py 負責；這個模組只管搜尋與播放控制。

指令：/play /pause /resume /skip /stop /queue /nowplaying /loop /volume /shuffle

支援平台：YouTube、YouTube Music、SoundCloud、Spotify（可貼網址，或用 /play 的「平台」欄位指定搜尋來源）。
⚠️ Spotify 需要 Lavalink 節點安裝 LavaSrc 外掛並設定 Spotify clientId / clientSecret；
   Spotify 本身不提供串流，LavaSrc 會找出對應的歌曲音源來播放。
（舊的 yt-dlp 版本已備份在 _old/music_ytdlp.py）
"""
import asyncio
import logging
import re
from typing import Literal, Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import config
from cogs.voice import always_on, prepare_player
from utils import error_embed, make_embed, respond, safe_send, success_embed, truncate

log = logging.getLogger("music")

# 友善名稱 → Lavalink 搜尋前綴；其他值（如 "spsearch"）原樣傳給節點
SEARCH_PREFIX = {"youtube": "ytsearch", "youtube_music": "ytmsearch", "soundcloud": "scsearch", "spotify": "spsearch"}
# /play「平台」欄位的選項 → 搜尋前綴
PLATFORMS = {"YouTube": "ytsearch", "YouTube Music": "ytmsearch", "SoundCloud": "scsearch", "Spotify": "spsearch"}
SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]+)$")
LOOP_MODES = {
    "關閉": wavelink.QueueMode.normal,
    "單曲": wavelink.QueueMode.loop,
    "整個佇列": wavelink.QueueMode.loop_all,
}
MODE_TEXT = {mode: text for text, mode in LOOP_MODES.items()}


def normalize_query(query: str) -> str:
    """把 spotify:track:ID 這種 URI 轉成網址（節點才認得）。"""
    m = SPOTIFY_URI_RE.match(query.strip())
    return f"https://open.spotify.com/{m.group(1)}/{m.group(2)}" if m else query.strip()


def is_url(query: str) -> bool:
    return query.lower().startswith(("http://", "https://"))


def fmt_duration(ms: int) -> str:
    seconds = max(int(ms) // 1000, 0)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def duration_text(track: wavelink.Playable) -> str:
    return "直播" if track.is_stream else fmt_duration(track.length)


def track_link(track: wavelink.Playable) -> str:
    title = truncate(track.title, 80)
    return f"[{title}]({track.uri})" if track.uri else title


class Music(commands.Cog, name="音樂"):
    """用關鍵字搜尋播放音樂（Lavalink 節點）"""

    emoji = "🎵"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.text_channels: dict[int, discord.abc.Messageable] = {}  # 發送「正在播放」通知的頻道
        self._idle_tasks: dict[int, asyncio.Task] = {}

    async def cog_unload(self):
        for task in self._idle_tasks.values():
            task.cancel()
        self._idle_tasks.clear()

    # ───────────────────── 輔助 ─────────────────────
    @staticmethod
    async def _has_lavasrc() -> Optional[bool]:
        """節點是否安裝了 LavaSrc（Spotify 需要）。True / False；查不到時回傳 None（未知）。"""
        try:
            info = await wavelink.Pool.get_node().fetch_info()
            names = [str(getattr(p, "name", "")).lower() for p in info.plugins]
            return any("lavasrc" in n for n in names)
        except Exception:
            return None

    async def _control(self, interaction: discord.Interaction):
        """控制類指令：機器人必須在語音頻道，且使用者要在同一個頻道。"""
        vc = interaction.guild.voice_client
        if not isinstance(vc, wavelink.Player) or vc.channel is None:
            await respond(interaction, embed=error_embed("我目前不在語音頻道，先用 `/play` 點首歌吧！"), ephemeral=True)
            return None
        if getattr(interaction.user.voice, "channel", None) != vc.channel:
            await respond(interaction, embed=error_embed(f"請先加入我所在的語音頻道 {vc.channel.mention}。"), ephemeral=True)
            return None
        return vc

    def _schedule_idle(self, player: wavelink.Player):
        """沒東西播放時，過一段時間自動離開（24/7 與 /join 模式除外）。"""
        delay = config.MUSIC_IDLE_LEAVE_SECONDS
        if not delay:
            return
        old = self._idle_tasks.pop(player.guild.id, None)
        if old:
            old.cancel()
        self._idle_tasks[player.guild.id] = asyncio.create_task(self._idle_leave(player, delay))

    async def _idle_leave(self, player: wavelink.Player, delay: int):
        try:
            await asyncio.sleep(delay)
            guild = player.guild
            if guild.voice_client is not player or not player.connected:
                return
            if player.playing or player.paused or len(player.queue) > 0:
                return
            if str(guild.id) in always_on(self.bot) or getattr(player, "stay", False):
                return
            channel = self.text_channels.get(guild.id)
            if channel:
                await safe_send(channel, embed=make_embed(description="💤 閒置太久了，我先離開語音頻道囉～", timestamp=False))
            await player.disconnect()
        except asyncio.CancelledError:
            pass

    # ───────────────────── Wavelink 事件 ─────────────────────
    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload):
        player = payload.player
        if player is None:
            return
        task = self._idle_tasks.pop(player.guild.id, None)
        if task:
            task.cancel()
        channel = self.text_channels.get(player.guild.id)
        if channel:
            track = payload.track
            embed = make_embed(
                "🎵 正在播放",
                f"**{track_link(track)}**\n{track.author or ''}",
                thumbnail=track.artwork,
                fields=[("時長", duration_text(track))],
            )
            await safe_send(channel, embed=embed)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload):
        # 若接著還有下一首，時間到時會發現正在播放而不會離開
        if payload.player is not None:
            self._schedule_idle(payload.player)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload):
        player = payload.player
        log.warning("播放失敗：%s", getattr(payload, "exception", None))
        channel = self.text_channels.get(player.guild.id) if player else None
        if channel:
            await safe_send(channel, embed=error_embed("這首歌無法播放（來源錯誤），已略過。"))

    # ───────────────────── 指令 ─────────────────────
    @app_commands.command(name="play", description="播放音樂（關鍵字或網址；支援 YouTube、Spotify、SoundCloud）")
    @app_commands.describe(query="歌名、關鍵字，或網址（含 Spotify 歌曲/專輯/播放清單連結）", platform="用關鍵字搜尋時的平台（預設見設定檔）")
    @app_commands.rename(platform="平台")
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
        platform: Optional[Literal["YouTube", "YouTube Music", "SoundCloud", "Spotify"]] = None,
    ):
        await interaction.response.defer()
        player = await prepare_player(interaction)
        if not player:
            return

        self.text_channels[interaction.guild_id] = interaction.channel
        if not getattr(player, "music_ready", False):
            player.music_ready = True
            try:
                await player.set_volume(config.MUSIC_DEFAULT_VOLUME)
            except Exception:
                log.warning("設定預設音量失敗", exc_info=True)

        room = config.MUSIC_MAX_QUEUE - len(player.queue)
        if room <= 0:
            return await interaction.followup.send(
                embed=error_embed(f"佇列已滿（上限 {config.MUSIC_MAX_QUEUE} 首）。"), ephemeral=True
            )

        query = normalize_query(query)
        if platform and not is_url(query):
            source = PLATFORMS[platform]  # 這次指定的平台
        else:
            source = SEARCH_PREFIX.get(config.MUSIC_SEARCH_SOURCE.lower(), config.MUSIC_SEARCH_SOURCE)
        is_spotify = source == "spsearch" and not is_url(query) or "open.spotify.com" in query.lower()

        if is_spotify and await self._has_lavasrc() is False:
            self._schedule_idle(player)
            return await interaction.followup.send(
                embed=error_embed(
                    "這個 Lavalink 節點不支援 Spotify。",
                    reason="Spotify 需要節點安裝 **LavaSrc** 外掛；機器人檢查後發現目前連線的節點沒有這個外掛。",
                    fix="• 請節點管理員安裝 LavaSrc 並設定 Spotify 的 clientId / clientSecret\n"
                        "• 或改用 `/play` 的「平台」欄位選 YouTube / SoundCloud\n"
                        "• 或在 `config.LAVALINK_NODES` 加入有安裝 LavaSrc 的節點",
                ),
                ephemeral=True,
            )

        try:
            results = await wavelink.Playable.search(query, source=source)
        except Exception as e:
            log.warning("搜尋失敗：%s", e)
            self._schedule_idle(player)
            if is_spotify:
                embed = error_embed(
                    "Spotify 搜尋 / 載入失敗。",
                    reason="常見原因：\n• 節點沒有安裝或沒設定好 LavaSrc（缺少 Spotify clientId / clientSecret）\n"
                           "• 連結是私人播放清單，或該內容在節點所在地區無法存取\n• Spotify 暫時限流",
                    fix="請確認連結是公開的；若一直失敗，請節點管理員檢查 LavaSrc 設定，或先改用 YouTube / SoundCloud。",
                    detail=f"{type(e).__name__}: {str(e)[:300]}",
                )
            else:
                embed = error_embed(
                    "搜尋失敗：節點無法載入這個來源。",
                    reason="音樂節點暫時連不上該平台、該平台限制了節點的 IP，或連結已失效。",
                    fix="請稍後再試、換個關鍵字，或用「平台」欄位改選其他平台。",
                    detail=f"{type(e).__name__}: {str(e)[:300]}",
                )
            return await interaction.followup.send(embed=embed, ephemeral=True)
        if not results:
            self._schedule_idle(player)
            return await interaction.followup.send(
                embed=error_embed(
                    "找不到相關歌曲。",
                    reason="這個平台上沒有符合的結果，或關鍵字太特殊、拼錯了。",
                    fix="試試換個關鍵字（歌名＋歌手），或用「平台」欄位換一個平台搜尋。",
                ),
                ephemeral=True,
            )

        if isinstance(results, wavelink.Playlist):
            tracks = list(results.tracks)[:room]
            player.queue.put(tracks)
            embed = make_embed("➕ 已加入播放清單", f"**{truncate(results.name, 100)}**", fields=[("曲目數", len(tracks))])
            first = tracks[0]
        else:
            first = results[0]
            player.queue.put(first)
            embed = make_embed(
                "➕ 已加入佇列",
                track_link(first),
                thumbnail=first.artwork,
                fields=[("時長", duration_text(first)), ("順位", f"第 {len(player.queue)} 首")],
            )

        if not player.playing and not player.paused:
            await player.play(player.queue.get())
            await interaction.followup.send(embed=success_embed(f"找到了：**{truncate(first.title, 100)}**"))
        else:
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="pause", description="暫停播放")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if player := await self._control(interaction):
            if player.playing:
                await player.pause(True)
                await interaction.response.send_message("⏸️ 已暫停")
            else:
                await interaction.response.send_message("目前沒有在播放", ephemeral=True)

    @app_commands.command(name="resume", description="繼續播放")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if player := await self._control(interaction):
            if player.paused:
                await player.pause(False)
                await interaction.response.send_message("▶️ 已繼續播放")
            else:
                await interaction.response.send_message("目前沒有暫停中的音樂", ephemeral=True)

    @app_commands.command(name="skip", description="跳過目前這首")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if player := await self._control(interaction):
            if player.current is None:
                return await interaction.response.send_message("目前沒有在播放", ephemeral=True)
            title = player.current.title
            await player.skip(force=True)
            await interaction.response.send_message(f"⏭️ 已跳過：**{truncate(title, 80)}**")

    @app_commands.command(name="stop", description="停止播放並清空佇列（不會離開語音頻道）")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if player := await self._control(interaction):
            player.queue.clear()
            player.queue.mode = wavelink.QueueMode.normal
            if player.current is not None:
                await player.skip(force=True)
            self._schedule_idle(player)
            await interaction.response.send_message("⏹️ 已停止播放並清空佇列（要讓我離開請用 `/leave`）")

    @app_commands.command(name="queue", description="查看播放佇列")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player) or (player.current is None and len(player.queue) == 0):
            return await interaction.response.send_message("佇列是空的，用 `/play` 點首歌吧！", ephemeral=True)

        lines = []
        if player.current:
            lines.append(f"▶️ **{truncate(player.current.title, 60)}** `{duration_text(player.current)}`")
        upcoming = list(player.queue)
        for i, track in enumerate(upcoming[:10], 1):
            lines.append(f"`{i}.` {truncate(track.title, 60)} `{duration_text(track)}`")
        if len(upcoming) > 10:
            lines.append(f"…還有 {len(upcoming) - 10} 首")
        embed = make_embed("🎶 播放佇列", "\n".join(lines), footer=f"循環模式：{MODE_TEXT.get(player.queue.mode, '關閉')}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="查看正在播放的歌曲")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client
        track = player.current if isinstance(player, wavelink.Player) else None
        if track is None:
            return await interaction.response.send_message("目前沒有在播放", ephemeral=True)

        if track.is_stream:
            bar = "🔴 直播中"
        else:
            filled = min(int(player.position / max(track.length, 1) * 15), 15)
            bar = f"{'▬' * filled}🔘{'▬' * (15 - filled)}\n`{fmt_duration(player.position)} / {fmt_duration(track.length)}`"
        embed = make_embed(
            "🎵 正在播放",
            f"**{track_link(track)}**\n{track.author or ''}\n\n{bar}",
            thumbnail=track.artwork,
            fields=[("音量", f"{player.volume}%"), ("狀態", "已暫停" if player.paused else "播放中")],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="設定循環播放")
    @app_commands.describe(mode="循環模式")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: Literal["關閉", "單曲", "整個佇列"]):
        if player := await self._control(interaction):
            player.queue.mode = LOOP_MODES[mode]
            await interaction.response.send_message(f"🔁 循環模式：**{mode}**")

    @app_commands.command(name="volume", description="調整音量")
    @app_commands.describe(percent="音量百分比（1-100）")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 100]):
        if player := await self._control(interaction):
            await player.set_volume(percent)
            await interaction.response.send_message(f"🔊 音量已設為 {percent}%")

    @app_commands.command(name="shuffle", description="打亂播放佇列")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if player := await self._control(interaction):
            if len(player.queue) < 2:
                return await interaction.response.send_message("佇列裡的歌不夠多，沒辦法打亂。", ephemeral=True)
            player.queue.shuffle()
            await interaction.response.send_message("🔀 已打亂播放佇列")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
