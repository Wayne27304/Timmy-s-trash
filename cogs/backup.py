"""伺服器備份與還原。
備份內容：身分組（含權限）、分類與頻道（含主題、慢速、NSFW、權限覆寫）、@everyone 權限。
不包含：訊息、成員、表情符號、貼圖、機器人與整合服務。
備份檔存在 backups/<伺服器ID>/，建立時也會把 .json 檔傳給你，方便另外保存。

自動備份：用 /backup_auto 設定（每個伺服器各自設定間隔與保留份數）。

還原模式：
  補齊 — 只建立「缺少」的身分組與頻道，不會刪除任何東西（所有管理員可用）
  取代 — 先刪除現有的頻道與身分組再依備份重建（僅伺服器擁有者可用；目前所在頻道會保留）
⚠️ 還原需要機器人擁有「管理員」權限：Discord 不允許沒有某項權限的機器人，建立帶有該項權限的身分組。
"""
import io
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import Colors, error_embed, fmt_tw, make_embed, now_tw, require_perms, safe_send, success_embed, warn_embed

log = logging.getLogger("backup")
BACKUP_DIR = Path("backups")
ID_RE = re.compile(r"^[\w-]+$")
VERSION = 1


# ───────────────────────── 建立快照 ─────────────────────────
def _overwrites(channel) -> list[dict]:
    items = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        if isinstance(target, discord.Role):
            name = "@everyone" if target.is_default() else target.name
            items.append({"type": "role", "id": target.id, "name": name, "allow": allow.value, "deny": deny.value})
        elif isinstance(target, discord.Member):
            items.append({"type": "member", "id": target.id, "name": str(target), "allow": allow.value, "deny": deny.value})
    return items


def snapshot(guild: discord.Guild, name: str = "", auto: bool = False) -> dict:
    roles = [
        {
            "id": r.id, "name": r.name, "color": r.colour.value, "hoist": r.hoist, "mentionable": r.mentionable,
            "permissions": r.permissions.value, "position": r.position,
        }
        for r in guild.roles
        if not r.is_default() and not r.managed
    ]
    categories = [
        {"id": c.id, "name": c.name, "position": c.position, "overwrites": _overwrites(c)} for c in guild.categories
    ]
    channels = []
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        entry = {
            "name": ch.name, "type": ch.type.name, "position": ch.position,
            "category_id": ch.category_id, "category": ch.category.name if ch.category else None,
            "overwrites": _overwrites(ch),
        }
        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            entry.update(topic=ch.topic, nsfw=ch.nsfw, slowmode=ch.slowmode_delay)
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            entry.update(user_limit=ch.user_limit)
        channels.append(entry)

    return {
        "version": VERSION,
        "name": name,
        "auto": auto,
        "created": now_tw().isoformat(),  # 台灣時間（+08:00）
        "guild": {"id": guild.id, "name": guild.name},
        "everyone_permissions": guild.default_role.permissions.value,
        "roles": roles,
        "categories": categories,
        "channels": channels,
    }


def _build_overwrites(guild: discord.Guild, items: list[dict], role_map: dict) -> dict:
    result = {}
    for o in items:
        if o["type"] == "role":
            target = guild.default_role if o["name"] == "@everyone" else (
                role_map.get(o["id"]) or discord.utils.get(guild.roles, name=o["name"])
            )
        else:
            target = guild.get_member(o["id"])
        if target is not None:
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(o["allow"]), discord.Permissions(o["deny"])
            )
    return result


# ───────────────────────── 還原確認 ─────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=45)
        self.user_id = user_id
        self.value = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="確認還原", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


# ───────────────────────── Cog ─────────────────────────
class Backup(commands.Cog, name="伺服器備份"):
    """備份與還原伺服器的身分組、頻道結構"""

    emoji = "💾"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.auto_backup.start()  # 每 5 分鐘檢查各伺服器的排程（沒開自動備份的伺服器會直接略過）

    async def cog_unload(self):
        self.auto_backup.cancel()

    # ───────── 檔案存取 ─────────
    @staticmethod
    def _dir(guild_id: int) -> Path:
        path = BACKUP_DIR / str(guild_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _save(self, guild_id: int, data: dict) -> str:
        backup_id = now_tw().strftime("%Y%m%d-%H%M%S")  # 台灣時間
        (self._dir(guild_id) / f"{backup_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return backup_id

    def _list(self, guild_id: int) -> list[tuple[str, dict]]:
        items = []
        for path in sorted(self._dir(guild_id).glob("*.json"), reverse=True):
            try:
                items.append((path.stem, json.loads(path.read_text("utf-8"))))
            except (OSError, ValueError):
                continue
        return items

    @staticmethod
    def _created_text(data: dict) -> str:
        """備份的建立時間（台灣時間）。舊備份存的是 UTC，也會正確換算。"""
        try:
            return fmt_tw(datetime.fromisoformat(data["created"]), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            return "時間未知"

    def _prune_auto(self, guild_id: int):
        autos = [bid for bid, d in self._list(guild_id) if d.get("auto")]
        for bid in autos[self.backup_cfg(guild_id)["keep"]:]:
            (self._dir(guild_id) / f"{bid}.json").unlink(missing_ok=True)

    async def _complete_ids(self, interaction: discord.Interaction, current: str):
        choices = []
        for bid, d in self._list(interaction.guild_id):
            label = f"{bid} {d.get('name') or ''}".strip()
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=bid))
        return choices[:25]

    # ───────── 自動備份（設定用 /backup_auto）─────────
    def backup_cfg(self, guild_id: int) -> dict:
        """該伺服器的自動備份設定。hours = 0 代表關閉；last = 上次自動備份的時間（epoch）。"""
        cfg = {"hours": 0, "keep": 5, "last": 0.0}
        cfg.update(self.bot.settings.get(guild_id, "backup", {}) or {})
        return cfg

    async def _update_cfg(self, guild_id: int, **values):
        stored = dict(self.bot.settings.get(guild_id, "backup", {}) or {})
        stored.update(values)
        await self.bot.settings.set(guild_id, "backup", stored)

    @tasks.loop(minutes=5)
    async def auto_backup(self):
        now = time.time()
        for guild in list(self.bot.guilds):
            cfg = self.backup_cfg(guild.id)
            if cfg["hours"] <= 0 or now - cfg["last"] < cfg["hours"] * 3600:
                continue
            try:
                self._save(guild.id, snapshot(guild, name="自動備份", auto=True))
                self._prune_auto(guild.id)
            except Exception:  # noqa: BLE001
                log.exception("自動備份 %s 失敗", guild.name)
            finally:
                await self._update_cfg(guild.id, last=now)  # 成功或失敗都等下一個間隔再試，避免狂刷錯誤

    @auto_backup.before_loop
    async def _before_auto(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="backup_auto", description="設定自動備份的間隔與保留份數（不填 = 查看目前設定）")
    @app_commands.describe(hours="每幾小時自動備份一次（0 = 關閉；最長 720）", keep="自動備份最多保留幾份（1-30；手動備份不受影響）")
    @app_commands.rename(hours="間隔小時", keep="保留份數")
    @require_perms(administrator=True)
    async def backup_auto(
        self,
        interaction: discord.Interaction,
        hours: Optional[app_commands.Range[int, 0, 720]] = None,
        keep: Optional[app_commands.Range[int, 1, 30]] = None,
    ):
        values = {}
        if hours is not None:
            values["hours"] = hours
            values["last"] = time.time()  # 從現在開始算，第一次備份在一個間隔之後
        if keep is not None:
            values["keep"] = keep
        if values:
            await self._update_cfg(interaction.guild_id, **values)
            self._prune_auto(interaction.guild_id)
        cfg = self.backup_cfg(interaction.guild_id)
        if cfg["hours"] > 0:
            schedule = f"每 **{cfg['hours']}** 小時一次，下次約 <t:{int(cfg['last'] + cfg['hours'] * 3600)}:R>"
        else:
            schedule = "**關閉**（用 `間隔小時` 欄位開啟，例如 24 = 每天一次）"
        embed = make_embed(
            "💾 自動備份設定" + ("已更新" if values else ""),
            fields=[("自動備份", schedule, False), ("保留份數", f"{cfg['keep']} 份（只計算自動備份）")],
            color=Colors.SUCCESS if values else None,
            footer="時間皆為台灣時間（UTC+8）",
        )
        await interaction.response.send_message(embed=embed, ephemeral=not values)

    # ───────── 指令：建立 / 列表 / 刪除 ─────────
    @app_commands.command(name="backup_create", description="建立伺服器備份")
    @app_commands.describe(name="備註名稱（可選）")
    @require_perms(administrator=True)
    async def backup_create(self, interaction: discord.Interaction, name: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        data = snapshot(guild, name=name or "")
        backup_id = self._save(guild.id, data)
        file = discord.File(
            io.BytesIO(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")),
            filename=f"backup-{guild.id}-{backup_id}.json",
        )
        embed = success_embed(
            f"備份完成：`{backup_id}`\n建立時間：{fmt_tw(now_tw())}（台灣時間）\n"
            f"身分組 {len(data['roles'])}・分類 {len(data['categories'])}・頻道 {len(data['channels'])}\n"
            "檔案也已附上，建議另外保存。"
        )
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @app_commands.command(name="backup_list", description="查看已儲存的備份")
    @require_perms(administrator=True)
    async def backup_list(self, interaction: discord.Interaction):
        items = self._list(interaction.guild_id)
        if not items:
            return await interaction.response.send_message(embed=warn_embed("目前沒有備份。"), ephemeral=True)
        lines = [
            f"`{bid}` {'🤖' if d.get('auto') else '👤'} {d.get('name') or ''}\n"
            f"└ {self._created_text(d)}・身分組 {len(d['roles'])}・頻道 {len(d['channels'])}"
            for bid, d in items[:15]
        ]
        embed = make_embed(f"💾 備份列表（{len(items)} 份）", "\n".join(lines), footer="時間皆為台灣時間（UTC+8）")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="backup_delete", description="刪除備份")
    @app_commands.describe(backup_id="備份 ID")
    @require_perms(administrator=True)
    async def backup_delete(self, interaction: discord.Interaction, backup_id: str):
        path = self._dir(interaction.guild_id) / f"{backup_id}.json"
        if not ID_RE.match(backup_id) or not path.exists():
            return await interaction.response.send_message(embed=error_embed("找不到這個備份。"), ephemeral=True)
        path.unlink()
        await interaction.response.send_message(embed=success_embed(f"已刪除備份 `{backup_id}`。"), ephemeral=True)

    backup_delete.autocomplete("backup_id")(_complete_ids)

    # ───────── 指令：還原 ─────────
    @app_commands.command(name="backup_restore", description="還原備份（已存備份 ID 或上傳備份檔）")
    @app_commands.describe(
        backup_id="已儲存的備份 ID",
        file="或上傳備份檔（.json），可用來複製到另一個伺服器",
        mode="補齊＝只建立缺少的；取代＝刪除現有頻道與身分組後重建（僅擁有者）",
    )
    @require_perms(administrator=True)
    async def backup_restore(
        self,
        interaction: discord.Interaction,
        backup_id: Optional[str] = None,
        file: Optional[discord.Attachment] = None,
        mode: Literal["補齊", "取代"] = "補齊",
    ):
        guild = interaction.guild
        replace = mode == "取代"
        await interaction.response.defer(ephemeral=True)

        if replace and interaction.user.id != guild.owner_id:
            return await interaction.followup.send(embed=error_embed("「取代」模式只有伺服器擁有者可以使用。"), ephemeral=True)

        # 讀取備份資料
        try:
            if file:
                if file.size > 5_000_000:
                    raise ValueError("備份檔太大")
                data = json.loads(await file.read())
            elif backup_id and ID_RE.match(backup_id):
                data = json.loads((self._dir(guild.id) / f"{backup_id}.json").read_text("utf-8"))
            else:
                return await interaction.followup.send(embed=error_embed("請提供 `backup_id` 或上傳 `file`。"), ephemeral=True)
            if data.get("version") != VERSION or "roles" not in data or "channels" not in data:
                raise ValueError("格式不正確")
        except (OSError, ValueError):
            return await interaction.followup.send(embed=error_embed("讀取備份失敗：找不到或格式不正確。"), ephemeral=True)

        # 確認
        warning = (
            "⚠️ **取代模式：會刪除目前所有頻道（保留這個頻道）與可管理的身分組！**" if replace
            else "補齊模式：只會新增缺少的身分組與頻道，不會刪除任何東西。"
        )
        confirm = make_embed(
            "💾 確認還原",
            f"{warning}\n\n來源：**{data.get('guild', {}).get('name', '未知')}** {data.get('name') or ''}\n"
            f"身分組 {len(data['roles'])}・分類 {len(data['categories'])}・頻道 {len(data['channels'])}",
            color=Colors.WARNING,
        )
        view = ConfirmView(interaction.user.id)
        await interaction.edit_original_response(embed=confirm, view=view)
        await view.wait()
        if not view.value:
            return await interaction.edit_original_response(embed=warn_embed("已取消還原。"), view=None)

        async def progress(text: str):
            try:
                await interaction.edit_original_response(embed=warn_embed(f"⏳ {text}"), view=None)
            except discord.HTTPException:
                pass

        stats = await self._restore(guild, data, replace, interaction.channel_id, progress)
        embed = make_embed(
            "✅ 還原完成",
            color=Colors.SUCCESS,
            fields=[
                ("新增身分組", stats["roles"]), ("新增頻道/分類", stats["channels"]),
                ("已存在而略過", stats["skipped"]), ("失敗", stats["failed"]),
            ],
        )
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:  # 互動已過期（還原太久）
            await safe_send(interaction.channel, embed=embed)

    backup_restore.autocomplete("backup_id")(_complete_ids)

    # ───────── 還原核心 ─────────
    async def _restore(self, guild: discord.Guild, data: dict, replace: bool, keep_channel_id: int, progress) -> dict:
        stats = {"roles": 0, "channels": 0, "skipped": 0, "failed": 0}
        reason = "伺服器備份還原"

        if replace:
            await progress("刪除現有頻道…")
            for ch in list(guild.channels):
                if ch.id == keep_channel_id:
                    continue
                try:
                    await ch.delete(reason=reason)
                except discord.HTTPException:
                    stats["failed"] += 1
            await progress("刪除現有身分組…")
            for role in list(guild.roles):
                if role.is_default() or role.managed or role >= guild.me.top_role:
                    continue
                try:
                    await role.delete(reason=reason)
                except discord.HTTPException:
                    stats["failed"] += 1

        # 身分組：由高到低建立（新身分組會出現在最底層，所以順序剛好對）
        await progress("建立身分組…")
        try:
            await guild.default_role.edit(permissions=discord.Permissions(data.get("everyone_permissions", 0)), reason=reason)
        except discord.HTTPException:
            pass
        existing_roles = {r.name: r for r in guild.roles}
        role_map: dict[int, discord.Role] = {}
        for r in sorted(data["roles"], key=lambda x: x["position"], reverse=True):
            exists = existing_roles.get(r["name"])
            if exists and not replace:
                role_map[r["id"]] = exists
                stats["skipped"] += 1
                continue
            try:
                role_map[r["id"]] = await guild.create_role(
                    name=r["name"], colour=discord.Colour(r["color"]), hoist=r["hoist"],
                    mentionable=r["mentionable"], permissions=discord.Permissions(r["permissions"]), reason=reason,
                )
                stats["roles"] += 1
            except discord.HTTPException:
                stats["failed"] += 1

        # 分類
        await progress("建立分類與頻道…")
        existing_cats = {c.name: c for c in guild.categories}
        cat_map: dict[int, discord.CategoryChannel] = {}
        for c in sorted(data["categories"], key=lambda x: x["position"]):
            if c["name"] in existing_cats and not replace:
                cat_map[c["id"]] = existing_cats[c["name"]]
                stats["skipped"] += 1
                continue
            try:
                cat_map[c["id"]] = await guild.create_category(
                    c["name"], overwrites=_build_overwrites(guild, c["overwrites"], role_map), reason=reason
                )
                stats["channels"] += 1
            except discord.HTTPException:
                stats["failed"] += 1

        # 頻道
        existing_ch = {
            (ch.name, ch.type.name, ch.category.name if ch.category else None)
            for ch in guild.channels if not isinstance(ch, discord.CategoryChannel)
        }
        for ch in sorted(data["channels"], key=lambda x: x["position"]):
            if not replace and (ch["name"], ch["type"], ch.get("category")) in existing_ch:
                stats["skipped"] += 1
                continue
            kwargs = {
                "name": ch["name"],
                "category": cat_map.get(ch.get("category_id")),
                "overwrites": _build_overwrites(guild, ch["overwrites"], role_map),
                "reason": reason,
            }
            try:
                kind = ch["type"]
                if kind in ("text", "news"):
                    await guild.create_text_channel(
                        topic=ch.get("topic"), nsfw=ch.get("nsfw", False), slowmode_delay=ch.get("slowmode", 0), **kwargs
                    )
                elif kind == "voice":
                    await guild.create_voice_channel(user_limit=ch.get("user_limit", 0), **kwargs)
                elif kind == "stage_voice":
                    await guild.create_stage_channel(**kwargs)
                elif kind == "forum":
                    await guild.create_forum(
                        topic=ch.get("topic") or "", nsfw=ch.get("nsfw", False), slowmode_delay=ch.get("slowmode", 0), **kwargs
                    )
                else:
                    stats["skipped"] += 1
                    continue
                stats["channels"] += 1
            except discord.HTTPException:
                stats["failed"] += 1
        return stats


async def setup(bot: commands.Bot):
    await bot.add_cog(Backup(bot))
