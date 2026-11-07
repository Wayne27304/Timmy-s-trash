"""黑名單（每個伺服器各自一份）。
· 被列入黑名單的人：無法使用機器人的斜線指令 / AI 聊天 / 數數字
· 依 config.BLACKLIST_ACTION 處置：ban（封鎖，也會預先封鎖不在伺服器的人）/ kick / none
· 黑名單成員之後再加入伺服器時，會自動再處置一次
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.moderation import Moderation
from utils import Colors, error_embed, make_embed, require_perms, send_log, success_embed, truncate


class Blacklist(commands.Cog, name="黑名單"):
    """黑名單：加入時自動處置，且無法使用機器人"""

    emoji = "⛔"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _entries(self, guild_id: int) -> dict:
        return self.bot.blacklist.data.setdefault(str(guild_id), {})

    async def _apply(self, guild: discord.Guild, user: discord.abc.User) -> tuple[str, bool]:
        """依設定處置，回傳（結果文字, 是否已封鎖）。"""
        action = config.BLACKLIST_ACTION
        member = guild.get_member(user.id)
        try:
            if action == "ban":
                await guild.ban(user, reason="黑名單")
                return "已封鎖", True
            if action == "kick" and member:
                await member.kick(reason="黑名單")
                return "已踢出", False
        except discord.HTTPException:
            return "處置失敗（我的權限或身分組位置不足）", False
        return "僅禁止使用機器人", False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        entry = self._entries(member.guild.id).get(str(member.id))
        if not entry:
            return
        result, _ = await self._apply(member.guild, member)
        await send_log(
            member.guild,
            make_embed(
                "⛔ 黑名單成員加入",
                color=Colors.ERROR,
                author=member,
                fields=[("原因", truncate(entry.get("reason", "—"), 300), False), ("處置", result)],
            ),
        )

    @app_commands.command(name="blacklist_add", description="加入黑名單")
    @app_commands.describe(user="要加入黑名單的使用者（不在伺服器也可以）", reason="原因")
    @require_perms(ban_members=True)
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.User, reason: str = "未提供原因"):
        guild = interaction.guild
        member = guild.get_member(user.id)
        if member and (err := Moderation.hierarchy_error(interaction, member)):
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)
        if user.id == self.bot.user.id:
            return await interaction.response.send_message(embed=error_embed("不能把我自己加入黑名單。"), ephemeral=True)

        await interaction.response.defer()
        result, banned = await self._apply(guild, user)
        self._entries(guild.id)[str(user.id)] = {
            "reason": reason,
            "by": interaction.user.id,
            "time": discord.utils.utcnow().isoformat(),
            "banned": banned,
        }
        await self.bot.blacklist.save()

        embed = make_embed(
            "⛔ 已加入黑名單",
            color=Colors.ERROR,
            thumbnail=user.display_avatar.url,
            fields=[
                ("對象", f"{user.mention} (`{user.id}`)"),
                ("執行者", interaction.user.mention),
                ("原因", reason, False),
                ("處置", result),
            ],
        )
        await interaction.followup.send(embed=embed)
        await send_log(guild, embed)

    @app_commands.command(name="blacklist_remove", description="移出黑名單")
    @app_commands.describe(user="要移出黑名單的使用者")
    @require_perms(ban_members=True)
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.User):
        guild = interaction.guild
        entry = self._entries(guild.id).pop(str(user.id), None)
        if not entry:
            return await interaction.response.send_message(embed=error_embed("這個使用者不在黑名單中。"), ephemeral=True)
        await self.bot.blacklist.save()

        await interaction.response.defer()
        note = ""
        if entry.get("banned"):  # 只解除「因黑名單而封鎖」的人
            try:
                await guild.unban(user, reason="移出黑名單")
                note = "\n已一併解除封鎖。"
            except discord.HTTPException:
                note = "\n（解除封鎖失敗，可能已不在封鎖名單中。）"
        embed = success_embed(f"已將 {user.mention} 移出黑名單。{note}")
        await interaction.followup.send(embed=embed)
        await send_log(guild, make_embed("✅ 移出黑名單", color=Colors.SUCCESS, description=None, fields=[
            ("對象", f"{user.mention} (`{user.id}`)"), ("執行者", interaction.user.mention)]))

    @app_commands.command(name="blacklist_list", description="查看黑名單")
    @require_perms(ban_members=True)
    async def blacklist_list(self, interaction: discord.Interaction):
        entries = self._entries(interaction.guild_id)
        if not entries:
            return await interaction.response.send_message(embed=success_embed("黑名單是空的。"), ephemeral=True)
        lines = [
            f"<@{uid}> (`{uid}`) — {truncate(e.get('reason', '—'), 60)}"
            for uid, e in list(entries.items())[-20:]
        ]
        embed = make_embed(f"⛔ 黑名單（{len(entries)} 人）", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Blacklist(bot))
