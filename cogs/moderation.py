"""管理與審核：踢出、封鎖、禁言、警告、清除訊息、慢速模式、鎖定頻道。
所有操作都會寫入紀錄頻道（用 /log_set 設定）。
"""
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import Colors, JsonStore, error_embed, make_embed, require_perms, send_log, success_embed

NO_REASON = "未提供原因"


class Moderation(commands.Cog, name="管理審核"):
    """踢出、封鎖、禁言、警告與頻道管理"""

    emoji = "🛡️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warns = JsonStore("data/warns.json")  # {guild_id: {user_id: [警告, ...]}}

    # ───────────────────── 輔助方法 ─────────────────────
    @staticmethod
    def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> str | None:
        """檢查身分組階級，回傳錯誤訊息；沒問題則回傳 None。"""
        guild, user = interaction.guild, interaction.user
        if target.id == user.id:
            return "你不能對自己執行這個操作。"
        if target.id == guild.owner_id:
            return "不能對伺服器擁有者執行這個操作。"
        if user.id != guild.owner_id and target.top_role >= user.top_role:
            return "對方的身分組不低於你，無法執行。"
        if target.top_role >= guild.me.top_role:
            return "對方的身分組不低於我，我無法執行。請把我的身分組往上移。"
        return None

    @staticmethod
    def _who(user: discord.abc.User) -> str:
        return f"{user.mention} (`{user.id}`)"

    @staticmethod
    async def _deny(interaction: discord.Interaction, text: str, *, reason: str = None, fix: str = None):
        await interaction.response.send_message(
            embed=error_embed(
                text,
                reason=reason or "Discord 的身分組階級規則：只能管理「最高身分組比自己低」的成員，且伺服器擁有者不能被管理。",
                fix=fix or "請確認對象的最高身分組低於你與機器人；必要時到「伺服器設定 → 身分組」把機器人的身分組往上拖。",
            ),
            ephemeral=True,
        )

    @staticmethod
    async def _dm(user: discord.abc.User, guild: discord.Guild, action: str, reason: str):
        embed = make_embed(
            f"你在 {guild.name} 被{action}",
            color=Colors.WARNING,
            fields=[("原因", reason, False)],
        )
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass  # 對方關閉私訊

    async def _finish(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        target: str,
        reason: Optional[str] = None,
        color: int = Colors.SUCCESS,
        extra=(),
        thumbnail: Optional[str] = None,
    ):
        """回覆執行者（需先 defer）並寫入紀錄頻道。"""
        fields = [("對象", target), ("執行者", interaction.user.mention)]
        if reason:
            fields.append(("原因", reason, False))
        fields.extend(extra)
        embed = make_embed(f"🛡️ {action}", color=color, fields=fields, thumbnail=thumbnail)
        await interaction.followup.send(embed=embed)
        await send_log(interaction.guild, embed)

    # ───────────────────── 踢出 / 封鎖 ─────────────────────
    @app_commands.command(name="kick", description="踢出成員")
    @app_commands.describe(member="要踢出的成員", reason="原因")
    @require_perms(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        if err := self.hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await interaction.response.defer()
        await self._dm(member, interaction.guild, "踢出", reason)
        await member.kick(reason=f"{reason} | 執行者：{interaction.user}")
        await self._finish(
            interaction, "已踢出成員", target=self._who(member), reason=reason,
            thumbnail=member.display_avatar.url,
        )

    @app_commands.command(name="ban", description="封鎖使用者（對方不在伺服器也可封鎖）")
    @app_commands.describe(user="要封鎖的使用者", reason="原因", delete_days="一併刪除對方過去幾天的訊息（0-7）")
    @require_perms(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = NO_REASON,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        guild = interaction.guild
        member = guild.get_member(user.id)
        if member and (err := self.hierarchy_error(interaction, member)):
            return await self._deny(interaction, err)
        await interaction.response.defer()
        if member:
            await self._dm(member, guild, "封鎖", reason)
        await guild.ban(
            user,
            reason=f"{reason} | 執行者：{interaction.user}",
            delete_message_seconds=delete_days * 86400,
        )
        await self._finish(
            interaction, "已封鎖使用者", target=self._who(user), reason=reason,
            color=Colors.ERROR, thumbnail=user.display_avatar.url,
        )

    @app_commands.command(name="unban", description="解除封鎖")
    @app_commands.describe(user_id="要解除封鎖的使用者 ID", reason="原因")
    @require_perms(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = NO_REASON):
        if not user_id.strip().isdigit():
            return await self._deny(interaction, "請輸入有效的使用者 ID（純數字）。")
        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"{reason} | 執行者：{interaction.user}")
        except discord.NotFound:
            return await interaction.followup.send(
                embed=error_embed("找不到此使用者，或對方不在封鎖名單中。"), ephemeral=True
            )
        await self._finish(interaction, "已解除封鎖", target=self._who(user), reason=reason)

    # ───────────────────── 禁言 ─────────────────────
    @app_commands.command(name="timeout", description="禁言成員")
    @app_commands.describe(member="要禁言的成員", minutes="禁言分鐘數（最長 28 天）", reason="原因")
    @require_perms(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str = NO_REASON,
    ):
        if err := self.hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await interaction.response.defer()
        await member.timeout(timedelta(minutes=minutes), reason=f"{reason} | 執行者：{interaction.user}")
        await self._finish(
            interaction, "已禁言成員", target=self._who(member), reason=reason,
            color=Colors.WARNING, extra=[("時長", f"{minutes} 分鐘")],
            thumbnail=member.display_avatar.url,
        )

    @app_commands.command(name="untimeout", description="解除禁言")
    @app_commands.describe(member="要解除禁言的成員")
    @require_perms(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        if err := self.hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await interaction.response.defer()
        await member.timeout(None, reason=f"解除禁言 | 執行者：{interaction.user}")
        await self._finish(interaction, "已解除禁言", target=self._who(member))

    # ───────────────────── 警告 ─────────────────────
    @app_commands.command(name="warn", description="警告成員（累積達上限會自動禁言）")
    @app_commands.describe(member="要警告的成員", reason="原因")
    @require_perms(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if err := self.hierarchy_error(interaction, member):
            return await self._deny(interaction, err)
        await interaction.response.defer()

        records = self.warns.data.setdefault(str(interaction.guild_id), {}).setdefault(str(member.id), [])
        records.append(
            {
                "reason": reason,
                "moderator": interaction.user.id,
                "time": discord.utils.utcnow().isoformat(),
            }
        )
        await self.warns.save()
        count = len(records)

        await self._dm(member, interaction.guild, f"警告（第 {count} 次）", reason)

        extra = [("累計警告", f"{count} 次")]
        mod = self.warn_cfg(interaction.guild_id)
        if mod["limit"] and count >= mod["limit"]:
            try:
                await member.timeout(timedelta(minutes=mod["timeout_minutes"]), reason="警告次數達上限")
                extra.append(("自動處置", f"禁言 {mod['timeout_minutes']} 分鐘"))
            except discord.HTTPException:
                pass

        await self._finish(
            interaction, "已警告成員", target=self._who(member), reason=reason,
            color=Colors.WARNING, extra=extra, thumbnail=member.display_avatar.url,
        )

    def warn_cfg(self, guild_id: int) -> dict:
        """該伺服器的警告設定（預設：累積 3 次自動禁言 60 分鐘）。"""
        cfg = {"limit": 3, "timeout_minutes": 60}
        cfg.update(self.bot.settings.get(guild_id, "moderation", {}) or {})
        return cfg

    @app_commands.command(name="warn_setup", description="設定警告累積幾次自動禁言（不填 = 查看目前設定）")
    @app_commands.describe(limit="警告累積幾次自動禁言（0 = 不自動處置）", timeout_minutes="自動禁言幾分鐘")
    @app_commands.rename(limit="警告上限", timeout_minutes="禁言分鐘")
    @require_perms(manage_guild=True)
    async def warn_setup(
        self,
        interaction: discord.Interaction,
        limit: Optional[app_commands.Range[int, 0, 50]] = None,
        timeout_minutes: Optional[app_commands.Range[int, 1, 40320]] = None,
    ):
        values = {k: v for k, v in {"limit": limit, "timeout_minutes": timeout_minutes}.items() if v is not None}
        if values:
            stored = dict(self.bot.settings.get(interaction.guild_id, "moderation", {}) or {})
            stored.update(values)
            await self.bot.settings.set(interaction.guild_id, "moderation", stored)
        cfg = self.warn_cfg(interaction.guild_id)
        rule = f"警告累積 **{cfg['limit']}** 次 → 自動禁言 **{cfg['timeout_minutes']}** 分鐘" if cfg["limit"] else "目前**不會**自動處置（上限為 0）"
        await interaction.response.send_message(
            embed=make_embed("⚠️ 警告設定" + ("已更新" if values else ""), rule, color=Colors.SUCCESS if values else None),
            ephemeral=not values,
        )

    @app_commands.command(name="warnings", description="查看成員的警告紀錄")
    @app_commands.describe(member="要查詢的成員")
    @require_perms(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        records = self.warns.data.get(str(interaction.guild_id), {}).get(str(member.id), [])
        if not records:
            return await interaction.response.send_message(embed=success_embed(f"{member.mention} 沒有任何警告 ✨"))
        lines = []
        for i, w in enumerate(records, 1):
            ts = int(datetime.fromisoformat(w["time"]).timestamp())
            lines.append(f"**{i}.** {w['reason']}\n└ <@{w['moderator']}> · <t:{ts}:R>")
        embed = make_embed(
            f"⚠️ {member.display_name} 的警告紀錄（{len(records)} 次）",
            "\n".join(lines[-10:]),
            color=Colors.WARNING,
            thumbnail=member.display_avatar.url,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearwarns", description="清除成員的所有警告")
    @app_commands.describe(member="要清除警告的成員")
    @require_perms(moderate_members=True)
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        guild_data = self.warns.data.get(str(interaction.guild_id), {})
        removed = len(guild_data.pop(str(member.id), []))
        await self.warns.save()
        await interaction.response.defer()
        await self._finish(interaction, "已清除警告", target=self._who(member), extra=[("清除數量", f"{removed} 次")])

    # ───────────────────── 頻道管理 ─────────────────────
    @app_commands.command(name="clear", description="批次刪除訊息")
    @app_commands.describe(amount="要檢查的訊息數量（1-100）", member="只刪除這位成員的訊息（可選）")
    @require_perms(manage_messages=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
        deleted = await interaction.channel.purge(limit=amount, check=check, reason=f"/clear 由 {interaction.user} 執行")
        await interaction.followup.send(embed=success_embed(f"已刪除 {len(deleted)} 則訊息。"), ephemeral=True)
        await send_log(
            interaction.guild,
            make_embed(
                "🧹 批次刪除訊息",
                color=Colors.WARNING,
                fields=[
                    ("頻道", interaction.channel.mention),
                    ("執行者", interaction.user.mention),
                    ("刪除數量", len(deleted)),
                    ("限定對象", member.mention if member else "全部"),
                ],
            ),
        )

    @app_commands.command(name="slowmode", description="設定頻道慢速模式")
    @app_commands.describe(seconds="秒數（0 = 關閉，最長 21600）")
    @require_perms(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.response.defer()
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"由 {interaction.user} 設定")
        await self._finish(
            interaction, "慢速模式已更新", target=interaction.channel.mention,
            extra=[("間隔", f"{seconds} 秒" if seconds else "已關閉")],
        )

    @app_commands.command(name="lock", description="鎖定頻道（@everyone 無法發言）")
    @require_perms(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        await self._set_lock(interaction, locked=True)

    @app_commands.command(name="unlock", description="解除頻道鎖定")
    @require_perms(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        await self._set_lock(interaction, locked=False)

    async def _set_lock(self, interaction: discord.Interaction, locked: bool):
        await interaction.response.defer()
        channel, everyone = interaction.channel, interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = False if locked else None
        await channel.set_permissions(everyone, overwrite=overwrite, reason=f"由 {interaction.user} 操作")
        await self._finish(
            interaction,
            "頻道已鎖定 🔒" if locked else "頻道已解鎖 🔓",
            target=channel.mention,
            color=Colors.WARNING if locked else Colors.SUCCESS,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
