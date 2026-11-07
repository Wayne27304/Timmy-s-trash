"""開發者指令（用「@機器人 + 關鍵字」觸發，不是斜線指令）：

  @機器人 開發者指令       顯示這份列表
  @機器人 邀請連結         取得機器人的邀請連結
  @機器人 離開 <伺服器ID>  讓機器人離開指定伺服器
  @機器人 伺服器列表       顯示機器人所在的所有伺服器
  @機器人 狀態             顯示 CPU、RAM、磁碟使用量

只有機器人擁有者能使用（config.OWNER_IDS，或 Discord 開發者後台的應用程式擁有者／團隊成員）。
其他人 @ 機器人時，訊息會照常交給 AI 聊天處理。
"""
import asyncio
import logging
import os
import platform
import re
from typing import Optional

import discord
import psutil
from discord.ext import commands

from errors import build_error_embed
from utils import Colors, error_embed, fmt_tw, make_embed, success_embed

log = logging.getLogger("developer")

COMMANDS = [
    ("開發者指令", "顯示這份指令列表"),
    ("邀請連結", "取得機器人的邀請連結"),
    ("離開 <伺服器ID>", "讓機器人離開指定的伺服器"),
    ("伺服器列表", "顯示機器人所在的所有伺服器"),
    ("狀態", "顯示主機 CPU、RAM、磁碟使用量"),
]

# 「建議權限」邀請連結包含的權限（足夠使用所有功能，但不是管理員）
RECOMMENDED_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True, attach_files=True,
    add_reactions=True, read_message_history=True, use_external_emojis=True, manage_messages=True,
    manage_roles=True, manage_channels=True, manage_nicknames=True, kick_members=True, ban_members=True,
    moderate_members=True, view_audit_log=True, connect=True, speak=True,
)
SCOPES = ("bot", "applications.commands")
PAGE_SIZE = 8


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def usage_bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {percent:.1f}%"


class GuildPages(discord.ui.View):
    """伺服器列表的分頁按鈕（持久化：目前頁數記在 Embed 頁尾，重啟後舊訊息仍可翻頁；只有開發者能操作）。"""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @property
    def page_count(self) -> int:
        return max((len(self.bot.guilds) - 1) // PAGE_SIZE, 0) + 1

    def build(self, page: int) -> discord.Embed:
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        page = max(0, min(page, self.page_count - 1))
        start = page * PAGE_SIZE
        lines = []
        for i, g in enumerate(guilds[start:start + PAGE_SIZE], start + 1):
            joined = fmt_tw(g.me.joined_at, "%Y-%m-%d") if g.me and g.me.joined_at else "未知"
            lines.append(
                f"**{i}. {discord.utils.escape_markdown(g.name)}**\n"
                f"└ ID `{g.id}`｜成員 {g.member_count}｜擁有者 <@{g.owner_id}>｜加入 {joined}"
            )
        total_users = sum(g.member_count or 0 for g in guilds)
        return make_embed(
            f"🌐 伺服器列表（共 {len(guilds)} 個）",
            "\n".join(lines) or "目前不在任何伺服器。",
            footer=f"第 {page + 1}/{self.page_count} 頁｜成員總數 {total_users}｜離開：@機器人 離開 <ID>",
        )

    @staticmethod
    def current_page(message: discord.Message) -> int:
        try:
            return int(re.search(r"第 (\d+)/\d+ 頁", message.embeds[0].footer.text).group(1)) - 1
        except (AttributeError, IndexError, ValueError):
            return 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("這個列表只有機器人開發者能操作。", fix="請聯絡機器人擁有者。"), ephemeral=True
            )
            return False
        return True

    async def _go(self, interaction: discord.Interaction, step: int):
        page = (self.current_page(interaction.message) + step) % self.page_count  # 首尾相接
        await interaction.response.edit_message(embed=self.build(page), view=self)

    @discord.ui.button(label="上一頁", emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="devguilds:prev")
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="下一頁", emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="devguilds:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, 1)


class Developer(commands.Cog, name="開發者"):
    """開發者專用：@機器人 + 關鍵字（不是斜線指令）"""

    emoji = "🧰"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.process = psutil.Process(os.getpid())
        self.process.cpu_percent(None)  # 先量一次，之後才有「這段期間」的數值
        self.handlers = {
            "開發者指令": self._help,
            "邀請連結": self._invite,
            "離開": self._leave,
            "伺服器列表": self._guilds,
            "狀態": self._status,
        }

    async def cog_load(self):
        self.bot.add_view(GuildPages(self.bot))  # 重啟後舊的伺服器列表仍可翻頁

    # ───────────────────── 辨識 ─────────────────────
    def parse(self, message: discord.Message) -> Optional[tuple[str, str]]:
        """訊息若是「@機器人 關鍵字 [參數]」就回傳 (關鍵字, 參數)，否則回傳 None。"""
        if not self.bot.user:
            return None
        pattern = rf"\s*<@!?{self.bot.user.id}>\s*(開發者指令|邀請連結|伺服器列表|狀態|離開)\s*(\d*)\s*"
        match = re.fullmatch(pattern, message.content)
        if not match:
            return None
        keyword, arg = match.groups()
        if arg and keyword != "離開":  # 只有「離開」會帶參數；其他帶了多餘的字就當一般聊天
            return None
        return keyword, arg

    async def is_dev_command(self, message: discord.Message) -> bool:
        """給 AI 聊天用：這是不是開發者的指令（是的話 AI 不要回應）。"""
        return self.parse(message) is not None and await self.bot.is_owner(message.author)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        parsed = self.parse(message)
        if not parsed or not await self.bot.is_owner(message.author):
            return
        keyword, arg = parsed
        try:
            await self.handlers[keyword](message, arg)
        except Exception as e:  # noqa: BLE001
            try:
                await message.reply(embed=build_error_embed(e, f"@機器人 {keyword}"), mention_author=False)
            except discord.HTTPException:
                log.warning("無法送出開發者指令的錯誤訊息", exc_info=True)

    # ───────────────────── 指令 ─────────────────────
    async def _help(self, message: discord.Message, arg: str):
        lines = [f"`@{self.bot.user.name} {name}`\n└ {desc}" for name, desc in COMMANDS]
        embed = make_embed(
            "🧰 開發者指令",
            "\n".join(lines),
            footer="只有機器人擁有者能使用｜在 config.OWNER_IDS 設定",
        )
        await message.reply(embed=embed, mention_author=False)

    async def _invite(self, message: discord.Message, arg: str):
        admin = discord.utils.oauth_url(
            self.bot.user.id, permissions=discord.Permissions(administrator=True), scopes=SCOPES
        )
        recommended = discord.utils.oauth_url(self.bot.user.id, permissions=RECOMMENDED_PERMISSIONS, scopes=SCOPES)
        embed = make_embed(
            "🔗 機器人邀請連結",
            thumbnail=self.bot.user.display_avatar.url,
            fields=[
                ("管理員權限（推薦）", f"防炸群、備份、身分組管理都能完整運作。\n[點我邀請]({admin})", False),
                ("建議權限（非管理員）", f"只開啟功能需要的權限。\n[點我邀請]({recommended})", False),
            ],
            footer="邀請時需要有該伺服器的「管理伺服器」權限",
        )
        await message.reply(embed=embed, mention_author=False)

    async def _leave(self, message: discord.Message, arg: str):
        if not arg:
            return await message.reply(
                embed=error_embed(
                    "你沒有指定要離開的伺服器 ID。",
                    reason="這個指令需要知道要離開哪一個伺服器。",
                    fix=f"用法：`@{self.bot.user.name} 離開 <伺服器ID>`（可用 `@{self.bot.user.name} 伺服器列表` 查看 ID）",
                ),
                mention_author=False,
            )
        guild = self.bot.get_guild(int(arg))
        if guild is None:
            return await message.reply(
                embed=error_embed(
                    f"找不到伺服器 `{arg}`。",
                    reason="機器人不在這個伺服器，或 ID 輸入錯誤。",
                    fix=f"請用 `@{self.bot.user.name} 伺服器列表` 確認機器人所在的伺服器與正確的 ID。",
                ),
                mention_author=False,
            )
        name, members = guild.name, guild.member_count
        here = message.guild is not None and message.guild.id == guild.id
        if here:  # 就是目前所在的伺服器：離開後就無法再發訊息，所以先回覆
            await message.reply(embed=success_embed(f"我即將離開 **{name}**，再見了 👋"), mention_author=False)
        await guild.leave()
        log.info("開發者 %s (%s) 讓機器人離開了 %s (%s)", message.author, message.author.id, name, arg)
        if not here:
            await message.reply(
                embed=success_embed(f"已離開 **{discord.utils.escape_markdown(name)}**（`{arg}`，原有 {members} 位成員）。"),
                mention_author=False,
            )

    async def _guilds(self, message: discord.Message, arg: str):
        view = GuildPages(self.bot)
        await message.reply(embed=view.build(0), view=view, mention_author=False)

    def _collect(self) -> dict:
        """蒐集系統資訊（會短暫阻塞，所以在執行緒裡跑）。"""
        cpu = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(os.getcwd())  # 機器人所在的磁碟
        with self.process.oneshot():
            proc_cpu = self.process.cpu_percent(None)
            proc_mem = self.process.memory_info().rss
            threads = self.process.num_threads()
        try:
            load = os.getloadavg()
        except (AttributeError, OSError):  # Windows 沒有
            load = None
        return {
            "cpu": cpu, "cores": psutil.cpu_count(logical=True), "load": load,
            "ram": vm, "disk": disk, "proc_cpu": proc_cpu, "proc_mem": proc_mem, "threads": threads,
        }

    async def _status(self, message: discord.Message, arg: str):
        async with message.channel.typing():
            d = await asyncio.to_thread(self._collect)
        ram, disk = d["ram"], d["disk"]
        load = f"｜負載 {d['load'][0]:.2f} / {d['load'][1]:.2f} / {d['load'][2]:.2f}" if d["load"] else ""
        embed = make_embed(
            "📊 系統狀態",
            color=Colors.SUCCESS if d["cpu"] < 85 and ram.percent < 90 and disk.percent < 90 else Colors.WARNING,
            fields=[
                ("🖥️ CPU", f"`{usage_bar(d['cpu'])}`\n{d['cores']} 核心{load}", False),
                ("🧠 RAM", f"`{usage_bar(ram.percent)}`\n{fmt_bytes(ram.used)} / {fmt_bytes(ram.total)}", False),
                ("💽 磁碟", f"`{usage_bar(disk.percent)}`\n{fmt_bytes(disk.used)} / {fmt_bytes(disk.total)}（剩 {fmt_bytes(disk.free)}）", False),
                ("🤖 機器人程序", f"CPU {d['proc_cpu']:.1f}%｜RAM {fmt_bytes(d['proc_mem'])}｜{d['threads']} 執行緒", False),
                ("⏱️ 已運行", discord.utils.format_dt(self.bot.start_time, "R")),
                ("📡 延遲", f"{round(self.bot.latency * 1000)} ms"),
                ("🌐 伺服器", f"{len(self.bot.guilds)} 個"),
                ("⚙️ 環境", f"{platform.system()} {platform.release()}｜Python {platform.python_version()}｜discord.py {discord.__version__}", False),
            ],
            footer="在容器（如 Pterodactyl）內，CPU / RAM / 磁碟可能反映整台主機的數值",
        )
        await message.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Developer(bot))
