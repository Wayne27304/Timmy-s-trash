"""機器人主程式：python bot.py"""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from errors import build_error_embed
from utils import GuildSettings, JsonStore, bind_settings, respond

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

EXTENSIONS = [
    "cogs.general",
    "cogs.events",
    "cogs.moderation",
    "cogs.interactive",
    "cogs.ai_chat",
    "cogs.counting",
    "cogs.voice",
    "cogs.music",
    "cogs.security",
    "cogs.backup",
    "cogs.verification",
    "cogs.blacklist",
    "cogs.checkin",
    "cogs.developer",
    "cogs.welcome",
]

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
PRESENCES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
}


class MyTree(app_commands.CommandTree):
    """所有斜線指令執行前的共用檢查：該伺服器黑名單內的人不能使用機器人。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if (
            interaction.guild_id
            and bot.is_blacklisted(interaction.guild_id, interaction.user.id)
            and not await bot.is_owner(interaction.user)
        ):
            raise app_commands.CheckFailure("你已被列入這個伺服器的黑名單，無法使用機器人。")
        return True


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # 歡迎/離開、成員紀錄、備份（需在開發者後台開啟）
        intents.message_content = True  # AI 聊天、數數字讀取訊息內容（需在開發者後台開啟）
        super().__init__(
            command_prefix=config.PREFIX,
            intents=intents,
            help_command=None,
            tree_cls=MyTree,
            owner_ids=set(config.OWNER_IDS) or None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.start_time = discord.utils.utcnow()
        self.extension_names = EXTENSIONS
        self.settings = GuildSettings("data/settings.json")   # 各伺服器設定（紀錄頻道、驗證、自動身分組…）
        bind_settings(self.settings)
        self.blacklist = JsonStore("data/blacklist.json")     # 各伺服器黑名單 {guild_id: {user_id: {...}}}
        self.voice_store = JsonStore("data/voice.json")       # 24/7 語音掛機頻道 {"247": {guild_id: channel_id}}
        self._synced = False
        self._status_custom, self._status_rotating = self._load_statuses()
        self._status_index = 0
        self.tree.on_error = self.on_tree_error

    # ───────── 共用 ─────────
    def is_blacklisted(self, guild_id: int, user_id: int) -> bool:
        return str(user_id) in self.blacklist.data.get(str(guild_id), {})

    # ───────── 啟動 ─────────
    async def setup_hook(self):
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("已載入 %s", ext)
            except Exception:  # 單一模組壞掉不影響其他功能
                log.exception("載入 %s 失敗", ext)
        if config.LAVALINK_NODES:
            self._lavalink_task = asyncio.create_task(self._connect_lavalink())
        else:
            log.info("未設定 LAVALINK_NODES，音樂與語音掛機功能暫停")

    async def _connect_lavalink(self):
        try:
            import wavelink
        except ImportError:
            log.error("尚未安裝 wavelink，請執行 pip install -r requirements.txt")
            return
        await self.wait_until_ready()
        nodes = [
            wavelink.Node(
                identifier=n.get("identifier"),
                uri=n["uri"],
                password=n["password"],
                inactive_player_timeout=None,   # 閒置離開由 voice.py / music.py 自己處理，才能做到 24/7
                inactive_channel_tokens=None,
            )
            for n in config.LAVALINK_NODES
        ]
        try:
            await wavelink.Pool.connect(nodes=nodes, client=self, cache_capacity=100)
        except Exception:
            log.exception("無法連線到 Lavalink 節點，請檢查 config.LAVALINK_NODES")

    async def on_ready(self):
        log.info("已登入：%s (%s)，位於 %d 個伺服器", self.user, self.user.id, len(self.guilds))
        await self.apply_status()
        if self._status_rotating and not self.rotate_status.is_running():
            self.rotate_status.start()

        # 一上線就自動全域同步斜線指令（只在第一次 ready 時執行）
        if not self._synced:
            self._synced = True
            try:
                synced = await self.tree.sync()
                log.info("已完成全域指令同步：%d 個斜線指令", len(synced))
            except discord.HTTPException:
                self._synced = False
                log.exception("斜線指令同步失敗，下次重新連線時會再試")

    # ───────── 狀態氣泡：custom 固定、其他類型輪播 ─────────
    @staticmethod
    def _load_statuses():
        custom, rotating = None, []
        for kind, text in config.STATUSES:
            kind = kind.lower()
            if kind == "custom":
                custom = custom or text          # 只取第一個 custom，固定顯示
            elif kind in ACTIVITY_TYPES:
                rotating.append((kind, text))    # 其他類型輪播
            else:
                log.warning("未知的狀態類型：%s（可用：custom/%s）", kind, "/".join(ACTIVITY_TYPES))
        return custom, rotating

    def _fmt(self, text: str) -> str:
        try:
            return text.format(
                guilds=len(self.guilds),
                users=sum(g.member_count or 0 for g in self.guilds),
                prefix=config.PREFIX,
            )
        except (KeyError, IndexError, ValueError):
            return text

    async def apply_status(self, advance: bool = False):
        """套用狀態：固定的 custom ＋ 目前輪到的那一個。advance=True 時換到下一個。"""
        activities = []
        if self._status_custom:
            activities.append(discord.CustomActivity(name=self._fmt(self._status_custom)))
        if self._status_rotating:
            kind, text = self._status_rotating[self._status_index % len(self._status_rotating)]
            activities.append(discord.Activity(type=ACTIVITY_TYPES[kind], name=self._fmt(text)))
            if advance:
                self._status_index += 1
        await self._set_presence(activities)

    async def _set_presence(self, activities: list):
        """同時送出多個 activity（固定的 custom ＋ 輪播中的那一個）。"""
        presence = config.STATUS_PRESENCE if config.STATUS_PRESENCE in PRESENCES else "online"
        try:
            await self.ws.send_as_json(
                {
                    "op": 3,
                    "d": {
                        "since": None,
                        "activities": [a.to_dict() for a in activities],
                        "afk": False,
                        "status": presence,
                    },
                }
            )
        except Exception:
            log.warning("多重狀態送出失敗，改用單一狀態", exc_info=True)
            await self.change_presence(status=PRESENCES[presence], activity=activities[-1] if activities else None)

    @tasks.loop(seconds=max(5, config.STATUS_INTERVAL))
    async def rotate_status(self):
        await self.apply_status(advance=True)

    @rotate_status.before_loop
    async def _before_rotate_status(self):
        await self.wait_until_ready()

    async def on_guild_join(self, guild: discord.Guild):
        await self.apply_status()  # 更新 {guilds} / {users}

    async def on_guild_remove(self, guild: discord.Guild):
        await self.apply_status()

    # ───────── 錯誤處理（詳細的錯誤 Embed 由 errors.py 產生）─────────
    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        where = f"/{interaction.command.qualified_name}" if interaction.command else ""
        try:
            await respond(interaction, embed=build_error_embed(error, where), ephemeral=True)
        except discord.HTTPException:
            log.warning("無法送出錯誤訊息（互動可能已逾時）", exc_info=True)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
            return  # 非擁有者執行擁有者指令時不回應，避免洩漏有哪些指令
        where = f"{config.PREFIX}{ctx.command.qualified_name}" if ctx.command else ""
        try:
            await ctx.send(embed=build_error_embed(error, where))
        except discord.HTTPException:
            log.warning("無法送出錯誤訊息", exc_info=True)


async def _view_on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
    """按鈕 / 選單出錯時，也回覆詳細的錯誤 Embed（而不是只顯示「互動失敗」）。"""
    try:
        await respond(interaction, embed=build_error_embed(error, "這個按鈕/選單"), ephemeral=True)
    except discord.HTTPException:
        log.warning("無法送出按鈕錯誤訊息", exc_info=True)


async def _modal_on_error(self, interaction: discord.Interaction, error: Exception) -> None:
    try:
        await respond(interaction, embed=build_error_embed(error, "這個表單"), ephemeral=True)
    except discord.HTTPException:
        log.warning("無法送出表單錯誤訊息", exc_info=True)


discord.ui.View.on_error = _view_on_error
discord.ui.Modal.on_error = _modal_on_error


def main():
    if not config.TOKEN or config.TOKEN.startswith("YOUR_"):
        raise SystemExit("請先在 config.py 填入 TOKEN。")
    MyBot().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
