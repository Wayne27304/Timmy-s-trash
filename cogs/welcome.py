"""歡迎 / 離開通知（每個伺服器各自設定，存在 data/settings.json）。

可自訂：開關、發送頻道、標題、內容（可換行、可用變數）、顏色、圖片、是否「提及」成員、
是否顯示成員頭像，以及（歡迎限定）私訊歡迎詞給新成員。

指令：/welcome_status 查看設定　/welcome_setup 開關與選項　/welcome_edit 編輯文字與圖片（表單）
      /welcome_preview 預覽　/welcome_reset 恢復預設
"""
import copy
import re
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import (
    Colors, error_embed, fmt_tw, make_embed, require_perms, respond, safe_send, success_embed, truncate,
)

KINDS = {"歡迎": "welcome", "離開": "leave"}
KIND_NAMES = {"welcome": "歡迎", "leave": "離開"}

DEFAULTS = {
    "welcome": {
        "enabled": True,
        "channel": 0,                      # 0 = 使用伺服器的系統訊息頻道
        "title": "🎉 歡迎加入 {server}！",
        "message": "歡迎 {user} 加入 **{server}**！你是第 **{count}** 位成員 🎊",
        "color": "#57F287",
        "image": "",                       # 大圖網址（空 = 不顯示）
        "avatar": True,                    # 右上角顯示成員頭像
        "mention": True,                   # 在 Embed 外面「提及」該成員（會通知到他）
        "dm": False,                       # 同時私訊歡迎詞給新成員
    },
    "leave": {
        "enabled": True,
        "channel": 0,                      # 0 = 沿用歡迎頻道，再沒有就用系統訊息頻道
        "title": "👋 成員離開",
        "message": "**{name}** 離開了 {server}，目前剩下 {count} 位成員。",
        "color": "#95A5A6",
        "image": "",
        "avatar": True,
        "mention": False,
        "dm": False,
    },
}

VARIABLES = (
    "`{user}` 提及成員　`{name}` 顯示名稱　`{username}` 帳號名稱　`{id}` 使用者 ID\n"
    "`{server}` 伺服器名稱　`{count}` 目前成員數　`{created}` 帳號建立（相對時間）\n"
    "`{joined}` 加入時間（相對）　`{date}` 現在的台灣時間\n"
    "也可直接寫 `<#頻道ID>`、`<@&身分組ID>` 來連到頻道或身分組。"
)
URL_RE = re.compile(r"https?://\S+")


def render(template: str, values: dict) -> str:
    """把 {變數} 換成實際內容；不認識的 {xxx} 原樣保留。同時支援以 \\n 表示換行。"""
    text = template.replace("\\n", "\n")
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


def variables(member: discord.Member, escape: bool) -> dict:
    guild = member.guild
    esc = discord.utils.escape_markdown if escape else (lambda s: s)
    now = discord.utils.utcnow()
    return {
        "user": member.mention,
        "name": esc(member.display_name),
        "username": esc(member.name),
        "id": str(member.id),
        "server": esc(guild.name),
        "count": str(guild.member_count),
        "created": discord.utils.format_dt(member.created_at, "R"),
        "joined": discord.utils.format_dt(member.joined_at or now, "R"),
        "date": fmt_tw(now, "%Y-%m-%d %H:%M"),
    }


def parse_color(text: str, default: int) -> int:
    try:
        return discord.Colour.from_str(text.strip()).value
    except (ValueError, AttributeError):
        return default


class NoticeModal(discord.ui.Modal):
    """用表單編輯標題、內容（可換行）、顏色與圖片。"""

    def __init__(self, cog: "Welcome", kind: str, cfg: dict):
        super().__init__(title=f"編輯{KIND_NAMES[kind]}通知", timeout=900)
        self.cog, self.kind = cog, kind
        self.title_in = discord.ui.TextInput(
            label="標題（可用變數）", default=cfg["title"], max_length=250, required=False,
            placeholder="例如：🎉 歡迎加入 {server}！",
        )
        self.message_in = discord.ui.TextInput(
            label="內容（可換行、可用變數）", style=discord.TextStyle.paragraph, default=cfg["message"],
            max_length=2000, required=True, placeholder="例如：歡迎 {user}！請先閱讀 <#規則頻道ID>",
        )
        self.color_in = discord.ui.TextInput(
            label="顏色（例如 #57F287）", default=cfg["color"], max_length=7, required=False,
        )
        self.image_in = discord.ui.TextInput(
            label="大圖網址（留空 = 不顯示圖片）", default=cfg["image"], max_length=500, required=False,
            placeholder="https://example.com/banner.png",
        )
        for item in (self.title_in, self.message_in, self.color_in, self.image_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        color = self.color_in.value.strip() or DEFAULTS[self.kind]["color"]
        image = self.image_in.value.strip()
        if parse_color(color, -1) == -1:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"顏色 `{truncate(color, 20)}` 格式不正確，設定沒有儲存。",
                    reason="顏色必須是 6 位數的十六進位色碼。",
                    fix="請重新執行 `/welcome_edit`，輸入像 `#57F287` 或 `#ff8800` 的格式。",
                ),
                ephemeral=True,
            )
        if image and not URL_RE.fullmatch(image):
            return await interaction.response.send_message(
                embed=error_embed(
                    "圖片網址格式不正確，設定沒有儲存。",
                    reason="網址必須以 `http://` 或 `https://` 開頭，且不能包含空白。",
                    fix="請重新執行 `/welcome_edit`，貼上圖片的直接連結（通常以 .png / .jpg / .gif 結尾）。",
                ),
                ephemeral=True,
            )
        await self.cog._update(
            interaction.guild_id, self.kind,
            title=self.title_in.value.strip(), message=self.message_in.value, color=color, image=image,
        )
        content, embed = self.cog.build(self.kind, interaction.user, self.cog.cfg(interaction.guild_id, self.kind))
        await interaction.response.send_message(
            content=f"{KIND_NAMES[self.kind]}通知已更新！預覽（用你自己示範）：" + (f"\n{content}" if content else ""),
            embed=embed,
            ephemeral=True,
        )


class Welcome(commands.Cog, name="歡迎離開"):
    """可自訂的歡迎 / 離開通知（訊息、圖片、提及成員…）"""

    emoji = "👋"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ───────────────────── 設定存取 ─────────────────────
    def cfg(self, guild_id: int, kind: str) -> dict:
        merged = dict(DEFAULTS[kind])
        merged.update((self.bot.settings.get(guild_id, "notices", {}) or {}).get(kind, {}))
        return merged

    async def _update(self, guild_id: int, kind: str, **values):
        stored = copy.deepcopy(self.bot.settings.get(guild_id, "notices", {}) or {})
        stored.setdefault(kind, {}).update(values)
        await self.bot.settings.set(guild_id, "notices", stored)

    # ───────────────────── 產生通知 ─────────────────────
    def build(self, kind: str, member: discord.Member, cfg: dict) -> tuple[Optional[str], discord.Embed]:
        """回傳 (Embed 外面的提及文字, Embed)。"""
        title = render(cfg["title"], variables(member, escape=False)).strip()
        message = render(cfg["message"], variables(member, escape=True)).strip()
        embed = make_embed(
            truncate(title, 250) or None,
            truncate(message, 4000) or None,
            color=parse_color(cfg["color"], Colors.PRIMARY),
            thumbnail=member.display_avatar.url if cfg["avatar"] else None,
            image=cfg["image"] or None,
        )
        return (member.mention if cfg["mention"] else None), embed

    def _channel(self, guild: discord.Guild, kind: str, cfg: dict) -> Optional[discord.TextChannel]:
        ids = [cfg["channel"]]
        if kind == "leave":
            ids.append(self.cfg(guild.id, "welcome")["channel"])  # 離開通知預設沿用歡迎頻道
        for channel_id in ids:
            channel = guild.get_channel(channel_id) if channel_id else None
            if isinstance(channel, discord.TextChannel):
                return channel
        return guild.system_channel

    async def notify(self, kind: str, member: discord.Member):
        guild = member.guild
        if self.bot.is_blacklisted(guild.id, member.id):
            return  # 黑名單成員由 blacklist 模組處理，不發通知
        cfg = self.cfg(guild.id, kind)
        if not cfg["enabled"]:
            return
        content, embed = self.build(kind, member, cfg)
        channel = self._channel(guild, kind, cfg)
        if channel:
            await safe_send(channel, content=content, embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        if kind == "welcome" and cfg["dm"] and not member.bot:
            try:
                await member.send(embed=embed)
            except discord.HTTPException:
                pass  # 對方關閉私訊

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.notify("welcome", member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.notify("leave", member)

    # ───────────────────── 指令 ─────────────────────
    def _summary(self, guild: discord.Guild, kind: str) -> str:
        cfg = self.cfg(guild.id, kind)
        channel = self._channel(guild, kind, cfg)
        flag = lambda on: "✅" if on else "❌"  # noqa: E731
        lines = [
            f"狀態：{flag(cfg['enabled'])}　頻道：{channel.mention if channel else '❗找不到可發送的頻道'}",
            f"提及成員：{flag(cfg['mention'])}　顯示頭像：{flag(cfg['avatar'])}"
            + (f"　私訊歡迎：{flag(cfg['dm'])}" if kind == "welcome" else ""),
            f"顏色：`{cfg['color']}`　大圖：{'✅ 有' if cfg['image'] else '無'}",
            f"標題：{truncate(cfg['title'], 80) or '（無）'}",
            f"內容：{truncate(cfg['message'], 160)}",
        ]
        return "\n".join(lines)

    @app_commands.command(name="welcome_status", description="查看歡迎 / 離開通知的設定與可用變數")
    @require_perms(manage_guild=True)
    async def welcome_status(self, interaction: discord.Interaction):
        embed = make_embed(
            "👋 歡迎 / 離開通知",
            fields=[
                ("🎉 歡迎", self._summary(interaction.guild, "welcome"), False),
                ("👋 離開", self._summary(interaction.guild, "leave"), False),
                ("可用變數", VARIABLES, False),
                ("調整方式", "`/welcome_setup` 開關/頻道/提及/頭像　`/welcome_edit` 文字與圖片　`/welcome_preview` 預覽　`/welcome_reset` 恢復預設", False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="welcome_setup", description="設定歡迎 / 離開通知的開關、頻道、提及與頭像（只會修改你填寫的項目）")
    @app_commands.describe(
        kind="要設定歡迎還是離開",
        enabled="是否啟用",
        channel="發送通知的頻道",
        mention="是否在 Embed 外面「提及」該成員（會通知到他）",
        avatar="是否顯示成員頭像（縮圖）",
        dm="（僅歡迎）是否同時私訊歡迎詞給新成員",
    )
    @app_commands.rename(kind="類型", enabled="啟用", channel="頻道", mention="提及成員", avatar="顯示頭像", dm="私訊歡迎")
    @require_perms(manage_guild=True)
    async def welcome_setup(
        self,
        interaction: discord.Interaction,
        kind: Literal["歡迎", "離開"],
        enabled: Optional[bool] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[bool] = None,
        avatar: Optional[bool] = None,
        dm: Optional[bool] = None,
    ):
        key = KINDS[kind]
        values = {
            k: v for k, v in
            {"enabled": enabled, "channel": channel.id if channel else None, "mention": mention, "avatar": avatar, "dm": dm}.items()
            if v is not None
        }
        if not values:
            return await interaction.response.send_message(
                embed=error_embed(
                    "你沒有填寫任何要修改的項目。",
                    reason="這個指令只會修改你有填寫的欄位，全部留空就沒有東西可以改。",
                    fix="請至少填寫一個欄位，例如「啟用」或「頻道」。要改文字與圖片請用 `/welcome_edit`。",
                ),
                ephemeral=True,
            )
        if "dm" in values and key == "leave":
            return await interaction.response.send_message(
                embed=error_embed(
                    "「私訊歡迎」只能用在歡迎通知。",
                    reason="離開的成員已經不在伺服器，機器人無法私訊他們。",
                    fix="請把「類型」改成「歡迎」，或拿掉「私訊歡迎」欄位。",
                ),
                ephemeral=True,
            )
        if channel:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message(
                    embed=error_embed(
                        f"我在 {channel.mention} 沒有足夠的權限，設定沒有儲存。",
                        reason="發送通知需要「檢視頻道」「發送訊息」「嵌入連結」三項權限。",
                        fix="請到該頻道的權限設定，允許機器人使用這三項後再執行一次。",
                    ),
                    ephemeral=True,
                )
        await self._update(interaction.guild_id, key, **values)
        await interaction.response.send_message(
            embed=make_embed(f"✅ {kind}通知已更新", self._summary(interaction.guild, key), color=Colors.SUCCESS),
            ephemeral=True,
        )

    @app_commands.command(name="welcome_edit", description="用表單編輯歡迎 / 離開通知的標題、內容、顏色與圖片")
    @app_commands.describe(kind="要編輯歡迎還是離開")
    @app_commands.rename(kind="類型")
    @require_perms(manage_guild=True)
    async def welcome_edit(self, interaction: discord.Interaction, kind: Literal["歡迎", "離開"]):
        key = KINDS[kind]
        await interaction.response.send_modal(NoticeModal(self, key, self.cfg(interaction.guild_id, key)))

    @app_commands.command(name="welcome_preview", description="預覽歡迎 / 離開通知（用你自己當示範）")
    @app_commands.describe(kind="要預覽歡迎還是離開")
    @app_commands.rename(kind="類型")
    @require_perms(manage_guild=True)
    async def welcome_preview(self, interaction: discord.Interaction, kind: Literal["歡迎", "離開"]):
        key = KINDS[kind]
        content, embed = self.build(key, interaction.user, self.cfg(interaction.guild_id, key))
        await respond(
            interaction,
            content=f"以下是{kind}通知的預覽（只有你看得到，不會真的發送）：" + (f"\n{content}" if content else ""),
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(name="welcome_reset", description="把歡迎 / 離開通知恢復成預設設定")
    @app_commands.describe(kind="要重設歡迎還是離開")
    @app_commands.rename(kind="類型")
    @require_perms(manage_guild=True)
    async def welcome_reset(self, interaction: discord.Interaction, kind: Literal["歡迎", "離開"]):
        key = KINDS[kind]
        stored = copy.deepcopy(self.bot.settings.get(interaction.guild_id, "notices", {}) or {})
        stored.pop(key, None)
        if stored:
            await self.bot.settings.set(interaction.guild_id, "notices", stored)
        else:
            await self.bot.settings.remove(interaction.guild_id, "notices")
        await interaction.response.send_message(embed=success_embed(f"已把{kind}通知恢復成預設設定。"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
