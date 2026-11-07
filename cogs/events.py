"""事件紀錄與歡迎 / 離開通知。

紀錄內容：成員加入/離開、訊息刪除/編輯、暱稱與身分組變更、語音進出、頻道建立/刪除。
透過機器人指令執行的管理操作（踢出、封鎖等）由 moderation.py 自行寫入紀錄。
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import (
    Colors, error_embed, log_channel_id, make_embed, require_perms, safe_send, send_log, success_embed, truncate,
)

fmt = discord.utils.format_dt


class Events(commands.Cog, name="事件紀錄"):
    """事件紀錄與歡迎 / 離開通知（自動運作）"""

    emoji = "📜"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    def _channel(guild: discord.Guild, channel_id: int):
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ───────────────────── 紀錄頻道設定 ─────────────────────
    @app_commands.command(name="log_set", description="設定機器人日誌（事件紀錄）要發送到哪個頻道")
    @app_commands.describe(channel="日誌頻道")
    @require_perms(manage_guild=True)
    async def log_set(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                embed=error_embed(f"我在 {channel.mention} 沒有「檢視頻道、發送訊息、嵌入連結」權限，請先開啟再設定。"),
                ephemeral=True,
            )
        await self.bot.settings.set(interaction.guild_id, "log_channel", channel.id)
        await safe_send(
            channel,
            embed=make_embed(
                "📜 日誌頻道已設定",
                f"之後的事件紀錄與管理操作都會發送到這裡。\n設定者：{interaction.user.mention}",
                color=Colors.SUCCESS,
            ),
        )
        await interaction.response.send_message(embed=success_embed(f"日誌頻道已設定為 {channel.mention}。"))

    @app_commands.command(name="log_off", description="關閉機器人日誌")
    @require_perms(manage_guild=True)
    async def log_off(self, interaction: discord.Interaction):
        had = log_channel_id(interaction.guild_id)
        await self.bot.settings.remove(interaction.guild_id, "log_channel")
        text = "已關閉機器人日誌。" if had else "這個伺服器原本就沒有設定日誌頻道。"
        await interaction.response.send_message(embed=success_embed(text))

    @app_commands.command(name="log_show", description="查看目前的日誌頻道")
    @require_perms(manage_guild=True)
    async def log_show(self, interaction: discord.Interaction):
        channel_id = log_channel_id(interaction.guild_id)
        if not channel_id:
            text = "尚未設定日誌頻道，請用 `/log_set` 設定。"
        elif interaction.guild.get_channel(channel_id) is None:
            text = "已設定的日誌頻道不存在（可能被刪除了），請用 `/log_set` 重新設定。"
        else:
            text = f"目前的日誌頻道：<#{channel_id}>"
        await interaction.response.send_message(embed=make_embed(description=text, timestamp=False), ephemeral=True)

    # ───────────────────── 成員加入 / 離開（日誌；歡迎通知在 welcome.py）─────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild

        if self.bot.is_blacklisted(guild.id, member.id):
            return  # 黑名單成員由 blacklist 模組處理，不發歡迎訊息

        await send_log(
            guild,
            make_embed(
                "📥 成員加入",
                color=Colors.SUCCESS,
                author=member,
                fields=[("帳號建立", fmt(member.created_at, "R")), ("目前成員數", guild.member_count)],
            ),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if self.bot.is_blacklisted(guild.id, member.id):
            return  # 被黑名單處置的成員不發離開通知

        roles = [r.mention for r in member.roles if r != guild.default_role]
        await send_log(
            guild,
            make_embed(
                "📤 成員離開",
                color=Colors.ERROR,
                author=member,
                fields=[
                    ("加入時間", fmt(member.joined_at, "R") if member.joined_at else "未知"),
                    ("身分組", truncate(" ".join(roles) or "無"), False),
                ],
            ),
        )

    # ───────────────────── 訊息 ─────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        content = message.content or "（無文字內容，可能是附件或 Embed）"
        fields = [("頻道", message.channel.mention), ("內容", truncate(content), False)]
        if message.attachments:
            fields.append(("附件", "\n".join(a.filename for a in message.attachments), False))
        await send_log(
            message.guild,
            make_embed("🗑️ 訊息已刪除", color=Colors.ERROR, author=message.author, fields=fields),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        await send_log(
            after.guild,
            make_embed(
                "✏️ 訊息已編輯",
                color=Colors.WARNING,
                author=after.author,
                fields=[
                    ("頻道", f"{after.channel.mention} · [跳到訊息]({after.jump_url})", False),
                    ("修改前", truncate(before.content or "（空）"), False),
                    ("修改後", truncate(after.content or "（空）"), False),
                ],
            ),
        )

    # ───────────────────── 成員 ─────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            await send_log(
                after.guild,
                make_embed(
                    "📝 暱稱變更",
                    author=after,
                    fields=[("修改前", before.nick or "（無）"), ("修改後", after.nick or "（無）")],
                ),
            )

        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            fields = []
            if added:
                fields.append(("➕ 新增", " ".join(r.mention for r in added), False))
            if removed:
                fields.append(("➖ 移除", " ".join(r.mention for r in removed), False))
            await send_log(after.guild, make_embed("🏷️ 身分組變更", author=after, fields=fields))

    # ───────────────────── 語音 ─────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return  # 只記錄進出，不記錄靜音等狀態變化
        if before.channel is None:
            text, color = f"加入 {after.channel.mention}", Colors.SUCCESS
        elif after.channel is None:
            text, color = f"離開 {before.channel.mention}", Colors.ERROR
        else:
            text, color = f"{before.channel.mention} ➜ {after.channel.mention}", Colors.PRIMARY
        await send_log(member.guild, make_embed("🔊 語音頻道", text, color=color, author=member))

    # ───────────────────── 頻道 ─────────────────────
    @staticmethod
    def _is_ticket(channel) -> bool:
        return (getattr(channel, "topic", None) or "").startswith("ticket:")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if self._is_ticket(channel):
            return
        await send_log(
            channel.guild,
            make_embed("📁 頻道已建立", f"{channel.mention}（{channel.type}）", color=Colors.SUCCESS),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if self._is_ticket(channel):
            return
        await send_log(
            channel.guild,
            make_embed("📁 頻道已刪除", f"`#{channel.name}`（{channel.type}）", color=Colors.ERROR),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
