"""防炸群：所有設定都用斜線指令調整，每個伺服器各自獨立（存在 data/settings.json）。
  · 防洗版 / 大量標註 / 邀請連結   → 刪訊息並禁言
  · 防突襲（短時間大量加入）        → 啟動防護期，期間新加入者直接踢出 / 封鎖 / 禁言
  · 防破壞 Anti-Nuke               → 偵測有人短時間大量刪頻道、刪身分組、封鎖成員，立即處置該帳號

設定指令：/security_status /security_toggle /security_spam /security_raid /security_nuke
          /security_whitelist /security_reset /raid_end
偵測與處置結果都會寫入日誌頻道（用 /log_set 設定）。

⚠️ 機器人需要「檢視審核日誌」「管理身分組」「踢出/封鎖成員」「禁言成員」權限，
   且機器人的身分組位置必須高於要處置的對象，否則會處置失敗。
"""
import asyncio
import copy
import logging
import re
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Literal, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from utils import Colors, error_embed, make_embed, require_perms, safe_send, send_log, success_embed

log = logging.getLogger("security")
INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)

# 預設值（沒用指令調整過的項目就用這些）
DEFAULTS = {
    "enabled": True,                       # 總開關
    "whitelist_users": [],                 # 白名單使用者 ID
    "whitelist_roles": [],                 # 白名單身分組 ID
    "spam": {
        "enabled": True,                   # 防洗版
        "seconds": 5,                      # 在這段秒數內…
        "messages": 6,                     # …發送這麼多則訊息 → 刪除並禁言
        "timeout_minutes": 10,             # 禁言時長（分鐘）
        "max_mentions": 5,                 # 單則訊息標註上限（0 = 關閉）
        "block_invites": True,             # 一般成員貼 Discord 邀請連結 → 刪除
    },
    "raid": {
        "enabled": True,                   # 防突襲
        "seconds": 10,                     # 在這段秒數內…
        "joins": 8,                        # …有這麼多人加入 → 視為突襲
        "action": "kick",                  # kick / ban / timeout
        "lockdown_minutes": 10,            # 突襲後，這段時間內加入的新成員也會被處置
    },
    "nuke": {
        "enabled": True,                   # 防破壞
        "seconds": 10,
        "threshold": 3,                    # 這段時間內達到幾次危險操作
        "action": "strip",                 # strip / kick / ban
    },
}

RAID_ACTIONS = {"踢出": "kick", "封鎖": "ban", "禁言": "timeout"}
RAID_ACTION_NAMES = {"kick": "踢出", "ban": "封鎖", "timeout": "禁言 1 小時"}
NUKE_ACTIONS = {"移除身分組": "strip", "踢出": "kick", "封鎖": "ban"}
NUKE_ACTION_NAMES = {"strip": "移除所有身分組", "kick": "踢出", "ban": "封鎖"}
# 功能名稱 → 設定位置（頂層 key，或 (區塊, key)）
FEATURES = {
    "總開關": ("enabled",),
    "防洗版": ("spam", "enabled"),
    "邀請連結過濾": ("spam", "block_invites"),
    "防突襲": ("raid", "enabled"),
    "防破壞": ("nuke", "enabled"),
}


class Security(commands.Cog, name="防炸群"):
    """防洗版、防突襲、防破壞（自動運作，用指令設定）"""

    emoji = "🔒"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.msg_log = defaultdict(deque)       # (guild, user) -> deque[(時間, 訊息)]
        self.join_log = defaultdict(deque)      # guild -> deque[(時間, 成員)]
        self.lockdown_until: dict[int, float] = {}
        self.lockdown_hits: dict[int, int] = defaultdict(int)
        self.nuke_log = defaultdict(deque)      # (guild, user) -> deque[時間]
        self.nuke_punished: dict[tuple, float] = {}

    # ───────────────────── 設定存取 ─────────────────────
    def cfg(self, guild_id: int) -> dict:
        """該伺服器目前的防炸群設定（預設值 + 指令調整過的項目）。"""
        merged = copy.deepcopy(DEFAULTS)
        stored = self.bot.settings.get(guild_id, "security", {}) or {}
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    async def _update(self, guild_id: int, section: Optional[str], **values):
        stored = copy.deepcopy(self.bot.settings.get(guild_id, "security", {}) or {})
        target = stored if section is None else stored.setdefault(section, {})
        target.update(values)
        await self.bot.settings.set(guild_id, "security", stored)

    # ───────────────────── 輔助 ─────────────────────
    def _exempt(self, member: discord.Member, cfg: dict) -> bool:
        """洗版類檢查的豁免：擁有者、機器人自己、白名單、管理員/板主。"""
        if member.id in (member.guild.owner_id, self.bot.user.id) or member.id in cfg["whitelist_users"]:
            return True
        if any(r.id in cfg["whitelist_roles"] for r in member.roles):
            return True
        perms = member.guild_permissions
        return perms.administrator or perms.manage_messages

    @staticmethod
    async def _delete(message: discord.Message):
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    # ───────────────────── 防洗版 / 標註 / 邀請 ─────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        cfg = self.cfg(message.guild.id)
        member = message.author
        if not cfg["enabled"] or not isinstance(member, discord.Member) or self._exempt(member, cfg):
            return
        spam = cfg["spam"]

        if spam["block_invites"] and INVITE_RE.search(message.content):
            await self._delete(message)
            await safe_send(message.channel, content=f"{member.mention} 這裡不允許發送邀請連結。", delete_after=8)
            return

        mentions = len({m.id for m in message.mentions}) + len(message.role_mentions)
        if spam["max_mentions"] and mentions >= spam["max_mentions"]:
            return await self._punish_spam(member, [message], f"單則訊息標註 {mentions} 個目標", spam["timeout_minutes"])

        if not spam["enabled"]:
            return
        now = time.monotonic()
        recent = self.msg_log[(message.guild.id, member.id)]
        recent.append((now, message))
        while recent and now - recent[0][0] > spam["seconds"]:
            recent.popleft()
        if len(recent) >= spam["messages"]:
            messages = [m for _, m in recent]
            recent.clear()
            await self._punish_spam(
                member, messages, f"{spam['seconds']} 秒內發送 {len(messages)} 則訊息", spam["timeout_minutes"]
            )

    async def _punish_spam(self, member: discord.Member, messages: list[discord.Message], why: str, minutes: int):
        for m in messages:
            await self._delete(m)
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"防洗版：{why}")
            result = f"已禁言 {minutes} 分鐘"
        except discord.HTTPException:
            result = "禁言失敗（權限或身分組位置不足）"
        channel = messages[-1].channel
        await safe_send(channel, content=f"{member.mention} 偵測到洗版行為，{result}。", delete_after=10)
        await send_log(
            member.guild,
            make_embed(
                "🚨 防洗版觸發",
                color=Colors.ERROR,
                author=member,
                fields=[("原因", why, False), ("處置", result), ("頻道", channel.mention)],
            ),
        )

    # ───────────────────── 防突襲 ─────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.cfg(guild.id)
        raid = cfg["raid"]
        if not (cfg["enabled"] and raid["enabled"]) or member.id in cfg["whitelist_users"]:
            return
        now = time.monotonic()
        joins = self.join_log[guild.id]
        joins.append((now, member))
        while joins and now - joins[0][0] > raid["seconds"]:
            joins.popleft()

        # 防護期間：新加入者直接處置
        if now < self.lockdown_until.get(guild.id, 0):
            self.lockdown_hits[guild.id] += 1
            return await self._raid_action(member, "突襲防護期間加入", raid["action"])

        # 偵測突襲
        if len(joins) >= raid["joins"]:
            members = [m for _, m in joins]
            joins.clear()
            self.lockdown_until[guild.id] = now + raid["lockdown_minutes"] * 60
            self.lockdown_hits[guild.id] = len(members)
            await send_log(
                guild,
                make_embed(
                    "🚨 偵測到突襲！",
                    f"{raid['seconds']} 秒內有 **{len(members)}** 人加入。\n"
                    f"已啟動 {raid['lockdown_minutes']} 分鐘防護，期間加入的新成員會被**{RAID_ACTION_NAMES.get(raid['action'], '踢出')}**。\n"
                    "可用 `/raid_end` 提前結束防護。",
                    color=Colors.ERROR,
                ),
            )
            for m in members:
                await self._raid_action(m, "疑似突襲", raid["action"])

    async def _raid_action(self, member: discord.Member, why: str, action: str):
        try:
            if action == "ban":
                await member.ban(reason=f"防突襲：{why}")
            elif action == "timeout":
                await member.timeout(timedelta(hours=1), reason=f"防突襲：{why}")
            else:
                await member.kick(reason=f"防突襲：{why}")
        except discord.HTTPException:
            log.warning("防突襲處置 %s 失敗（權限不足？）", member)

    # ───────────────────── 防破壞 Anti-Nuke ─────────────────────
    async def _audit_executor(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int):
        await asyncio.sleep(1)  # 等審核日誌寫入
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                recent = (discord.utils.utcnow() - entry.created_at).total_seconds() < 15
                if recent and entry.target is not None and entry.target.id == target_id:
                    return entry.user_id
        except discord.HTTPException:
            log.warning("無法讀取審核日誌（缺少「檢視審核日誌」權限？），防破壞功能失效")
        return None

    async def _nuke_check(self, guild: discord.Guild, what: str, action: discord.AuditLogAction, target_id: int):
        cfg = self.cfg(guild.id)
        nuke = cfg["nuke"]
        if not (cfg["enabled"] and nuke["enabled"]):
            return
        uid = await self._audit_executor(guild, action, target_id)
        if uid is None or uid in (guild.owner_id, self.bot.user.id) or uid in cfg["whitelist_users"]:
            return

        now = time.monotonic()
        acts = self.nuke_log[(guild.id, uid)]
        acts.append(now)
        while acts and now - acts[0] > nuke["seconds"]:
            acts.popleft()

        key = (guild.id, uid)
        if len(acts) >= nuke["threshold"] and now - self.nuke_punished.get(key, 0) > 60:
            self.nuke_punished[key] = now
            await self._nuke_punish(guild, uid, what, len(acts), nuke)

    async def _nuke_punish(self, guild: discord.Guild, uid: int, what: str, count: int, nuke: dict):
        member = guild.get_member(uid)
        action = nuke["action"]
        if member and member.bot and action == "strip":
            action = "kick"  # 機器人的身分組多半是整合管理的，無法移除，改為踢出

        result = "無法處置（找不到該成員）"
        try:
            if action == "ban":
                await guild.ban(discord.Object(id=uid), reason="防破壞：短時間內大量破壞操作")
                result = "已封鎖"
            elif member and action == "kick":
                await member.kick(reason="防破壞：短時間內大量破壞操作")
                result = "已踢出"
            elif member:
                roles = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
                await member.remove_roles(*roles, reason="防破壞：短時間內大量破壞操作")
                result = f"已移除 {len(roles)} 個身分組"
        except discord.HTTPException:
            result = "❗處置失敗：我的身分組位置低於對方，或缺少權限，請立即手動處理！"

        embed = make_embed(
            "🚨 防破壞觸發",
            f"<@{uid}> 在 {nuke['seconds']} 秒內執行了 **{count}** 次危險操作（最近一次：{what}）。",
            color=Colors.ERROR,
            fields=[("處置", result, False)],
        )
        await send_log(guild, embed)
        if guild.owner:  # 同時私訊伺服器擁有者
            try:
                await guild.owner.send(content=f"**{guild.name}** 發生疑似破壞行為：", embed=embed)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self._nuke_check(channel.guild, "刪除頻道", discord.AuditLogAction.channel_delete, channel.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self._nuke_check(role.guild, "刪除身分組", discord.AuditLogAction.role_delete, role.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        await self._nuke_check(guild, "封鎖成員", discord.AuditLogAction.ban, user.id)

    # ───────────────────── 設定指令 ─────────────────────
    @staticmethod
    def _summary(cfg: dict) -> list[tuple]:
        flag = lambda on: "✅" if on else "❌"  # noqa: E731
        spam, raid, nuke = cfg["spam"], cfg["raid"], cfg["nuke"]
        users = " ".join(f"<@{u}>" for u in cfg["whitelist_users"][:15]) or "無"
        roles = " ".join(f"<@&{r}>" for r in cfg["whitelist_roles"][:15]) or "無"
        return [
            ("總開關", flag(cfg["enabled"])),
            ("防洗版", f"{flag(spam['enabled'])} {spam['seconds']} 秒 {spam['messages']} 則 → 禁言 {spam['timeout_minutes']} 分", False),
            ("大量標註", f"上限 {spam['max_mentions']} 個" if spam["max_mentions"] else "❌ 關閉"),
            ("邀請連結過濾", flag(spam["block_invites"])),
            ("防突襲", f"{flag(raid['enabled'])} {raid['seconds']} 秒 {raid['joins']} 人 → {RAID_ACTION_NAMES.get(raid['action'], '踢出')}"
                       f"（防護期 {raid['lockdown_minutes']} 分）", False),
            ("防破壞", f"{flag(nuke['enabled'])} {nuke['seconds']} 秒 {nuke['threshold']} 次 → {NUKE_ACTION_NAMES.get(nuke['action'], '移除身分組')}", False),
            ("白名單使用者", users, False),
            ("白名單身分組", roles, False),
        ]

    @app_commands.command(name="security_status", description="查看防炸群設定與目前狀態")
    @require_perms(manage_guild=True)
    async def security_status(self, interaction: discord.Interaction):
        cfg = self.cfg(interaction.guild_id)
        left = self.lockdown_until.get(interaction.guild_id, 0) - time.monotonic()
        state = f"⚠️ 防護中（剩 {int(left)} 秒，已處置 {self.lockdown_hits[interaction.guild_id]} 人）" if left > 0 else "正常"
        embed = make_embed(
            "🔒 防炸群狀態",
            "用 `/security_toggle`、`/security_spam`、`/security_raid`、`/security_nuke`、`/security_whitelist` 調整。",
            fields=self._summary(cfg) + [("突襲防護狀態", state, False)],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="security_toggle", description="開啟或關閉防炸群的某項功能")
    @app_commands.describe(feature="要調整的功能", enabled="開啟或關閉")
    @require_perms(administrator=True)
    async def security_toggle(
        self,
        interaction: discord.Interaction,
        feature: Literal["總開關", "防洗版", "邀請連結過濾", "防突襲", "防破壞"],
        enabled: bool,
    ):
        path = FEATURES[feature]
        if len(path) == 1:
            await self._update(interaction.guild_id, None, **{path[0]: enabled})
        else:
            await self._update(interaction.guild_id, path[0], **{path[1]: enabled})
        await interaction.response.send_message(embed=success_embed(f"已{'開啟' if enabled else '關閉'}「{feature}」。"))

    @app_commands.command(name="security_spam", description="設定防洗版、大量標註的門檻（只會修改你填寫的項目）")
    @app_commands.describe(
        messages="幾則訊息算洗版（2-30）",
        seconds="在幾秒內（1-60）",
        timeout_minutes="禁言幾分鐘",
        max_mentions="單則訊息標註上限（0 = 關閉標註偵測）",
    )
    @require_perms(administrator=True)
    async def security_spam(
        self,
        interaction: discord.Interaction,
        messages: Optional[app_commands.Range[int, 2, 30]] = None,
        seconds: Optional[app_commands.Range[int, 1, 60]] = None,
        timeout_minutes: Optional[app_commands.Range[int, 1, 40320]] = None,
        max_mentions: Optional[app_commands.Range[int, 0, 50]] = None,
    ):
        values = {
            k: v for k, v in
            {"messages": messages, "seconds": seconds, "timeout_minutes": timeout_minutes, "max_mentions": max_mentions}.items()
            if v is not None
        }
        if not values:
            return await interaction.response.send_message(
                embed=error_embed(
                    "你沒有填寫任何要修改的項目。",
                    reason="這個指令只會修改你有填寫的欄位，全部留空就沒有東西可以改。",
                    fix="請在指令中至少填寫一個欄位，例如 `messages`（幾則）與 `seconds`（幾秒內）。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, "spam", **values)
        s = self.cfg(interaction.guild_id)["spam"]
        await interaction.response.send_message(embed=success_embed(
            f"防洗版已更新：{s['seconds']} 秒內 {s['messages']} 則 → 禁言 {s['timeout_minutes']} 分鐘；"
            f"標註上限 {s['max_mentions'] or '（關閉）'}。"
        ))

    @app_commands.command(name="security_raid", description="設定防突襲的門檻與處置（只會修改你填寫的項目）")
    @app_commands.describe(
        joins="幾人加入算突襲（2-100）",
        seconds="在幾秒內（1-120）",
        lockdown_minutes="防護期幾分鐘（1-1440）",
        action="對突襲者的處置",
    )
    @require_perms(administrator=True)
    async def security_raid(
        self,
        interaction: discord.Interaction,
        joins: Optional[app_commands.Range[int, 2, 100]] = None,
        seconds: Optional[app_commands.Range[int, 1, 120]] = None,
        lockdown_minutes: Optional[app_commands.Range[int, 1, 1440]] = None,
        action: Optional[Literal["踢出", "封鎖", "禁言"]] = None,
    ):
        values = {
            k: v for k, v in
            {"joins": joins, "seconds": seconds, "lockdown_minutes": lockdown_minutes,
             "action": RAID_ACTIONS.get(action)}.items()
            if v is not None
        }
        if not values:
            return await interaction.response.send_message(
                embed=error_embed(
                    "你沒有填寫任何要修改的項目。",
                    reason="這個指令只會修改你有填寫的欄位，全部留空就沒有東西可以改。",
                    fix="請至少填寫一個欄位，例如 `joins`（幾人）、`seconds`（幾秒內）或 `action`（處置方式）。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, "raid", **values)
        r = self.cfg(interaction.guild_id)["raid"]
        await interaction.response.send_message(embed=success_embed(
            f"防突襲已更新：{r['seconds']} 秒內 {r['joins']} 人加入 → {RAID_ACTION_NAMES.get(r['action'], '踢出')}，"
            f"防護期 {r['lockdown_minutes']} 分鐘。"
        ))

    @app_commands.command(name="security_nuke", description="設定防破壞的門檻與處置（只會修改你填寫的項目）")
    @app_commands.describe(
        threshold="幾次危險操作算破壞（2-20）",
        seconds="在幾秒內（1-120）",
        action="對破壞者的處置",
    )
    @require_perms(administrator=True)
    async def security_nuke(
        self,
        interaction: discord.Interaction,
        threshold: Optional[app_commands.Range[int, 2, 20]] = None,
        seconds: Optional[app_commands.Range[int, 1, 120]] = None,
        action: Optional[Literal["移除身分組", "踢出", "封鎖"]] = None,
    ):
        values = {
            k: v for k, v in
            {"threshold": threshold, "seconds": seconds, "action": NUKE_ACTIONS.get(action)}.items()
            if v is not None
        }
        if not values:
            return await interaction.response.send_message(
                embed=error_embed(
                    "你沒有填寫任何要修改的項目。",
                    reason="這個指令只會修改你有填寫的欄位，全部留空就沒有東西可以改。",
                    fix="請至少填寫一個欄位，例如 `threshold`（幾次）、`seconds`（幾秒內）或 `action`（處置方式）。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, "nuke", **values)
        n = self.cfg(interaction.guild_id)["nuke"]
        await interaction.response.send_message(embed=success_embed(
            f"防破壞已更新：{n['seconds']} 秒內 {n['threshold']} 次危險操作 → {NUKE_ACTION_NAMES.get(n['action'], '移除身分組')}。\n"
            "提醒：此功能需要機器人有「檢視審核日誌」權限。"
        ))

    @app_commands.command(name="security_whitelist", description="新增或移除防炸群白名單（使用者或身分組）")
    @app_commands.describe(action="新增或移除", target="要加入/移除白名單的成員或身分組")
    @require_perms(administrator=True)
    async def security_whitelist(
        self,
        interaction: discord.Interaction,
        action: Literal["新增", "移除"],
        target: Union[discord.Member, discord.Role],
    ):
        if isinstance(target, discord.Role) and target.is_default():
            return await interaction.response.send_message(
                embed=error_embed(
                    "不能把 @everyone 加入白名單。",
                    reason="@everyone 代表所有人，加入後整個防炸群等於失效。",
                    fix="請改選特定的成員或身分組。",
                ),
                ephemeral=True,
            )
        key = "whitelist_roles" if isinstance(target, discord.Role) else "whitelist_users"
        ids = list(self.cfg(interaction.guild_id)[key])
        if action == "新增" and target.id not in ids:
            ids.append(target.id)
        elif action == "移除" and target.id in ids:
            ids.remove(target.id)
        else:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"{target.mention} {'已經在' if action == '新增' else '不在'}白名單中。",
                    reason="白名單沒有變動。",
                    fix="可用 `/security_status` 查看目前的白名單。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, None, **{key: ids})
        await interaction.response.send_message(embed=success_embed(f"已{action} {target.mention}（白名單）。"))

    @app_commands.command(name="security_reset", description="把這個伺服器的防炸群設定全部恢復成預設值")
    @require_perms(administrator=True)
    async def security_reset(self, interaction: discord.Interaction):
        await self.bot.settings.remove(interaction.guild_id, "security")
        await interaction.response.send_message(embed=success_embed("已恢復成預設設定（白名單也已清空）。"))

    @app_commands.command(name="raid_end", description="提前結束突襲防護期")
    @require_perms(manage_guild=True)
    async def raid_end(self, interaction: discord.Interaction):
        self.lockdown_until.pop(interaction.guild_id, None)
        self.lockdown_hits.pop(interaction.guild_id, None)
        await interaction.response.send_message(embed=success_embed("已結束突襲防護，新成員可正常加入。"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Security(bot))
