"""共用工具：Embed 產生器、權限裝飾器、JSON 儲存、紀錄頻道發送。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

import config


# 台灣時區（UTC+8，台灣沒有日光節約時間，不需要 tzdata）
TAIWAN_TZ = timezone(timedelta(hours=8), "Asia/Taipei")


def now_tw() -> datetime:
    return datetime.now(TAIWAN_TZ)


def fmt_tw(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把時間（任何時區）轉成台灣時間的字串。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIWAN_TZ).strftime(fmt)


PERM_NAMES = {
    "administrator": "管理員", "manage_guild": "管理伺服器", "manage_roles": "管理身分組",
    "manage_channels": "管理頻道", "manage_messages": "管理訊息", "manage_nicknames": "管理暱稱",
    "manage_webhooks": "管理 Webhook", "manage_threads": "管理討論串", "manage_expressions": "管理表情符號與貼圖",
    "kick_members": "踢出成員", "ban_members": "封鎖成員", "moderate_members": "禁言成員",
    "view_audit_log": "檢視審核日誌", "view_channel": "檢視頻道", "send_messages": "發送訊息",
    "send_messages_in_threads": "在討論串發送訊息", "embed_links": "嵌入連結", "attach_files": "附加檔案",
    "read_message_history": "讀取歷史訊息", "add_reactions": "新增反應", "mention_everyone": "標註 @everyone",
    "use_external_emojis": "使用外部表情符號", "use_application_commands": "使用應用程式指令",
    "create_instant_invite": "建立邀請", "change_nickname": "變更暱稱", "connect": "連線（語音）",
    "speak": "說話（語音）", "mute_members": "將成員靜音", "deafen_members": "讓成員聽不到", "move_members": "移動成員",
}


def perm_text(names) -> str:
    """把權限代碼轉成「中文（`代碼`）」的說明文字。"""
    return "、".join(f"{PERM_NAMES.get(n, n)}（`{n}`）" for n in names) or "（未知）"


class Colors:
    PRIMARY = config.EMBED_COLOR
    SUCCESS = 0x57F287
    ERROR = 0xED4245
    WARNING = 0xFEE75C
    NEUTRAL = 0x95A5A6


# ───────────────────────── Embed ─────────────────────────
def make_embed(
    title: Optional[str] = None,
    description: Optional[str] = None,
    *,
    color: Optional[int] = None,
    fields=None,          # [(name, value) 或 (name, value, inline), ...]
    thumbnail: Optional[str] = None,
    image: Optional[str] = None,
    author: Optional[discord.abc.User] = None,
    footer: Optional[str] = None,
    timestamp: bool = True,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else Colors.PRIMARY,
    )
    if timestamp:
        embed.timestamp = discord.utils.utcnow()
    for name, value, *rest in fields or []:
        embed.add_field(name=name, value=str(value) or "​", inline=rest[0] if rest else True)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    if author:
        embed.set_author(name=f"{author.display_name} ({author.id})", icon_url=author.display_avatar.url)
    embed.set_footer(text=footer or config.EMBED_FOOTER)
    return embed


def success_embed(text: str) -> discord.Embed:
    return make_embed(description=f"✅ {text}", color=Colors.SUCCESS, timestamp=False)


def error_embed(
    text: str,
    *,
    title: str = "❌ 操作失敗",
    reason: Optional[str] = None,
    fix: Optional[str] = None,
    detail: Optional[str] = None,
    error_id: Optional[str] = None,
) -> discord.Embed:
    """詳細的錯誤訊息：發生什麼事 → 可能原因 → 解決方式 → 技術細節（供回報用）。"""
    embed = make_embed(title, text, color=Colors.ERROR, timestamp=False)
    if reason:
        embed.add_field(name="📋 可能原因", value=truncate(reason, 1000), inline=False)
    if fix:
        embed.add_field(name="🛠️ 解決方式", value=truncate(fix, 1000), inline=False)
    if detail:
        embed.add_field(name="🔎 技術細節", value=f"```{truncate(detail, 900)}```", inline=False)
    if error_id:
        embed.set_footer(text=f"錯誤代碼：{error_id}（回報問題時請提供）")
    return embed


def warn_embed(text: str) -> discord.Embed:
    return make_embed(description=f"⚠️ {text}", color=Colors.WARNING, timestamp=False)


def info_embed(text: str) -> discord.Embed:
    return make_embed(description=text, timestamp=False)


# ───────────────────────── 文字 ─────────────────────────
def truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_message(text: str, limit: int = 1900) -> list[str]:
    """把長文字切成多段（盡量在換行處切開）。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


# ───────────────────────── 權限 ─────────────────────────
def require_perms(**perms: bool):
    """斜線指令用：限伺服器使用、預設只對有權限者顯示，並實際檢查權限。"""

    def decorator(func):
        func = app_commands.checks.has_permissions(**perms)(func)
        func = app_commands.default_permissions(**perms)(func)
        func = app_commands.guild_only()(func)
        return func

    return decorator


# ───────────────────────── 發送 / 紀錄 ─────────────────────────
async def safe_send(channel, **kwargs):
    try:
        return await channel.send(**kwargs)
    except discord.HTTPException:
        return None


_settings = None  # 由 bot.py 啟動時綁定（各伺服器設定）


def bind_settings(settings) -> None:
    global _settings
    _settings = settings


def log_channel_id(guild_id: int) -> int:
    """該伺服器用 /log_set 設定的紀錄頻道 ID（沒設定回傳 0）。"""
    return (_settings.get(guild_id, "log_channel", 0) or 0) if _settings else 0


async def send_log(guild: discord.Guild, embed: discord.Embed):
    """把 embed 送到該伺服器用 /log_set 設定的紀錄頻道。"""
    channel_id = log_channel_id(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        await safe_send(channel, embed=embed)


# ───────────────────────── JSON 儲存 ─────────────────────────
class JsonStore:
    """很簡單的 JSON 檔案儲存（適合小型資料，如警告紀錄）。"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.data: dict = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    async def save(self):
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)


class GuildSettings(JsonStore):
    """各伺服器的設定：{guild_id: {key: value}}"""

    def get(self, guild_id: int, key: str, default=None):
        return self.data.get(str(guild_id), {}).get(key, default)

    async def set(self, guild_id: int, key: str, value):
        self.data.setdefault(str(guild_id), {})[key] = value
        await self.save()

    async def remove(self, guild_id: int, key: str):
        self.data.get(str(guild_id), {}).pop(key, None)
        await self.save()


async def respond(interaction, **kwargs):
    """不論是否已 defer 都能正確回覆（kwargs 同 send_message，例如 embed=、ephemeral=、view=）。"""
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    return await interaction.response.send_message(**kwargs)
