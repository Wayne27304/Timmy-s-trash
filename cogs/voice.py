"""語音掛機：/join、/leave、/247（單一開關）
語音連線由 Lavalink（wavelink）負責，因此需要先在 config.LAVALINK_NODES 設定節點。

  /join  ─ 加入你所在的語音頻道，所有真人都離開後自動離開
  /247   ─ 24/7 永久掛機的「單一開關」：關閉時輸入 = 開啟，開啟時再輸入一次 = 關閉
          （重啟後自動回到該頻道；被踢出也會自動重連）
"""
import asyncio
import logging
from typing import Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import config
from utils import Colors, JsonStore, error_embed, make_embed, require_perms, respond, success_embed

log = logging.getLogger("voice")


# ───────────────────────── 共用函式（music.py 也會用）─────────────────────────
def always_on(bot: commands.Bot) -> dict:
    """{guild_id: channel_id}：永久掛機的頻道"""
    return bot.voice_store.data.setdefault("247", {})


def node_ready() -> bool:
    try:
        return any(n.status is wavelink.NodeStatus.CONNECTED for n in wavelink.Pool.nodes.values())
    except Exception:
        return False


async def try_connect(channel) -> tuple[Optional[wavelink.Player], Optional[str]]:
    """加入（或移動到）語音頻道。回傳 (player, 錯誤訊息)。"""
    guild = channel.guild
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.connect):
        return None, f"我沒有權限加入 {channel.mention}。"
    try:
        vc = guild.voice_client
        if isinstance(vc, wavelink.Player):
            if vc.channel is None or vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc, None
        if vc is not None:
            await vc.disconnect(force=True)
        player = await channel.connect(cls=wavelink.Player, self_deaf=True)
        player.autoplay = wavelink.AutoPlayMode.partial  # 佇列有歌就自動播下一首（不自動推薦）
        return player, None
    except asyncio.TimeoutError:
        return None, "連線語音頻道逾時，請稍後再試。"
    except Exception:
        log.exception("加入語音頻道失敗")
        return None, "無法加入語音頻道，請稍後再試。"


async def prepare_player(interaction: discord.Interaction) -> Optional[wavelink.Player]:
    """確認使用者在語音頻道、節點可用，並取得 player。失敗時已回覆錯誤並回傳 None。"""
    bot = interaction.client
    guild = interaction.guild
    user_channel = getattr(interaction.user.voice, "channel", None)

    if user_channel is None:
        await respond(interaction, embed=error_embed("請先加入一個語音頻道。"), ephemeral=True)
        return None
    if not node_ready():
        await respond(
            interaction,
            embed=error_embed("語音節點（Lavalink）尚未連線，請管理員檢查 `config.LAVALINK_NODES`。"),
            ephemeral=True,
        )
        return None

    vc = guild.voice_client
    if vc is not None and vc.channel is not None and vc.channel.id != user_channel.id:
        humans = [m for m in vc.channel.members if not m.bot]
        if humans or str(guild.id) in always_on(bot):
            await respond(
                interaction,
                embed=error_embed(f"我正在 {vc.channel.mention} 服務中，請加入該頻道。"),
                ephemeral=True,
            )
            return None

    player, err = await try_connect(user_channel)
    if err:
        await respond(interaction, embed=error_embed(err), ephemeral=True)
    return player


# ───────────────────────── Cog ─────────────────────────
class Voice(commands.Cog, name="語音掛機"):
    """讓機器人待在語音頻道：/join 跟隨離開、/247 永久掛機"""

    emoji = "🎙️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(bot, "voice_store"):
            bot.voice_store = JsonStore("data/voice.json")
        self._restore_lock = asyncio.Lock()

    # ───────── 指令 ─────────
    @app_commands.command(name="join", description="加入你的語音頻道（真人都離開後會自動離開）")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player = await prepare_player(interaction)
        if not player:
            return
        player.stay = True  # /join：只在真人都離開時才離開（不受閒置計時影響）
        if str(interaction.guild_id) in always_on(self.bot):
            text = f"已在 {player.channel.mention}（此伺服器已啟用 24/7 掛機，我不會自動離開）。"
        else:
            text = f"已加入 {player.channel.mention}，所有人都離開後我會自動離開。"
        await interaction.followup.send(embed=success_embed(text))

    @app_commands.command(name="247", description="24/7 永久掛機開關（開啟時再輸入一次即可關閉）")
    @app_commands.describe(channel="要掛機的語音頻道（預設是你目前所在的頻道；已開啟時指定其他頻道可直接換頻道）")
    @require_perms(manage_guild=True)
    async def always_on_cmd(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel] = None):
        guild = interaction.guild
        store = always_on(self.bot)
        current_id = store.get(str(guild.id))

        # ── 目前是開啟狀態，而且沒有指定「別的頻道」→ 把開關關掉 ──
        if current_id and (channel is None or channel.id == current_id):
            store.pop(str(guild.id), None)
            await self.bot.voice_store.save()
            vc = guild.voice_client
            if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
                await vc.disconnect()
                text = "🔴 24/7 掛機已**關閉**，頻道裡已經沒有人，我也離開語音頻道了。"
            elif vc and vc.channel:
                text = f"🔴 24/7 掛機已**關閉**。我會先待在 {vc.channel.mention}，等所有真人離開後自動離開。"
            else:
                text = "🔴 24/7 掛機已**關閉**。"
            return await respond(interaction, embed=make_embed(description=text, color=Colors.NEUTRAL, timestamp=False))

        # ── 關閉 → 開啟（或已開啟時換到別的頻道）──
        target = channel or getattr(interaction.user.voice, "channel", None)
        if target is None:
            return await respond(
                interaction,
                embed=error_embed(
                    "我不知道要掛機在哪個語音頻道。",
                    reason="你沒有指定頻道，而且你目前也不在任何語音頻道裡。",
                    fix="請先加入想讓我掛機的語音頻道再輸入 `/247`，或在指令的 `channel` 欄位選一個語音頻道。",
                ),
                ephemeral=True,
            )
        if not node_ready():
            return await respond(
                interaction,
                embed=error_embed(
                    "語音節點（Lavalink）尚未連線，無法掛機。",
                    reason="機器人連不上 `config.LAVALINK_NODES` 設定的音樂節點（節點關閉、網址或密碼錯誤、剛啟動還在連線）。",
                    fix="請管理員檢查節點設定與機器人的 Console 紀錄；節點連上後再試一次。",
                ),
                ephemeral=True,
            )
        await interaction.response.defer()
        player, err = await try_connect(target)
        if err:
            return await interaction.followup.send(
                embed=error_embed(err, fix="請確認我有「檢視頻道」與「連線」權限，且頻道沒有滿員。"), ephemeral=True
            )

        store[str(guild.id)] = target.id
        await self.bot.voice_store.save()
        text = (
            f"🟢 24/7 掛機已**開啟**：{target.mention}。重啟、被踢出都會自動回來；再輸入一次 `/247` 即可關閉。"
            if not current_id else f"🟢 24/7 掛機仍為**開啟**，已改為掛機在 {target.mention}。"
        )
        await interaction.followup.send(embed=success_embed(text))

    @app_commands.command(name="leave", description="讓機器人離開語音頻道")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        vc = guild.voice_client
        if vc is None:
            return await respond(interaction, embed=error_embed("我目前不在任何語音頻道。"), ephemeral=True)

        is_admin = interaction.user.guild_permissions.manage_guild
        same_channel = getattr(interaction.user.voice, "channel", None) == vc.channel
        if not (is_admin or same_channel):
            return await respond(
                interaction, embed=error_embed("你必須和我在同一個語音頻道才能使用。"), ephemeral=True
            )

        if str(guild.id) in always_on(self.bot):
            if not is_admin:
                return await respond(
                    interaction, embed=error_embed("此伺服器啟用了 24/7 掛機，只有管理員能讓我離開。"), ephemeral=True
                )
            always_on(self.bot).pop(str(guild.id), None)
            await self.bot.voice_store.save()

        await vc.disconnect()
        await interaction.response.send_message(embed=success_embed("已離開語音頻道。"))

    # ───────── 事件 ─────────
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        log.info("Lavalink 節點已就緒：%s", payload.node.identifier)
        await self.bot.wait_until_ready()
        await self.restore_always_on()

    async def restore_always_on(self):
        """啟動 / 節點重連後，回到所有 24/7 的語音頻道。"""
        async with self._restore_lock:
            for guild_id, channel_id in list(always_on(self.bot).items()):
                guild = self.bot.get_guild(int(guild_id))
                channel = guild.get_channel(channel_id) if guild else None
                if not isinstance(channel, discord.VoiceChannel) or guild.voice_client is not None:
                    continue
                _, err = await try_connect(channel)
                if err:
                    log.warning("24/7 重連 %s 失敗：%s", channel_id, err)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild

        # 機器人自己被踢出 / 斷線 → 24/7 模式自動重連
        if member.id == self.bot.user.id:
            channel_id = always_on(self.bot).get(str(guild.id))
            if channel_id and before.channel is not None and after.channel is None:
                await asyncio.sleep(3)
                channel = guild.get_channel(channel_id)
                if guild.voice_client is None and isinstance(channel, discord.VoiceChannel):
                    await try_connect(channel)
            return

        # 非 24/7：所屬頻道沒有真人了 → 離開
        vc = guild.voice_client
        if vc is None or vc.channel is None or str(guild.id) in always_on(self.bot):
            return
        if before.channel is None or before.channel.id != vc.channel.id or after.channel == before.channel:
            return
        await asyncio.sleep(config.VOICE_EMPTY_LEAVE_DELAY)
        vc = guild.voice_client
        if vc is None or vc.channel is None or str(guild.id) in always_on(self.bot):
            return
        if not [m for m in vc.channel.members if not m.bot]:
            await vc.disconnect()


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
