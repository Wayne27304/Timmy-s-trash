"""一般指令：/help（選單）、/ping、/botinfo、/userinfo、/serverinfo、/avatar、/embed"""
import platform
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import Colors, error_embed, make_embed, require_perms

fmt = discord.utils.format_dt


# ───────────────────────── /help 選單（持久化：重啟後舊訊息的選單仍可使用）─────────────────────────
class HelpSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        options = [
            discord.SelectOption(
                label=name,
                value=name,
                description=(cog.description or "")[:100] or None,
                emoji=getattr(cog, "emoji", None),
            )
            for name, cog in bot.cogs.items()
            if cog.get_app_commands()
        ] or [discord.SelectOption(label="載入中", value="loading")]
        super().__init__(custom_id="help:category", placeholder="選擇指令分類…", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        cog = self.bot.get_cog(self.values[0])
        if cog is None:
            return await interaction.response.send_message(
                embed=error_embed(
                    "找不到這個指令分類。",
                    reason="機器人剛更新過，這則舊訊息的分類清單已經過期。",
                    fix="請重新輸入一次 `/help`，用新的選單查看。",
                ),
                ephemeral=True,
            )
        lines = [f"`/{c.name}` — {c.description}" for c in cog.get_app_commands()]
        embed = make_embed(f"{getattr(cog, 'emoji', '📁')} {cog.qualified_name}", "\n".join(lines) or "（沒有指令）")
        # 用只有自己看得到的訊息回覆，這樣公開的 /help 訊息不會被別人的選擇改掉
        await interaction.response.send_message(embed=embed, ephemeral=True)


class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.add_item(HelpSelect(bot))


# ───────────────────────── Cog ─────────────────────────
class General(commands.Cog, name="一般"):
    """資訊查詢與實用指令"""

    emoji = "✨"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(HelpView(self.bot))  # 重啟後舊的 /help 訊息仍可使用

    @app_commands.command(name="help", description="顯示指令列表")
    async def help(self, interaction: discord.Interaction):
        fields = [
            (f"{getattr(cog, 'emoji', '📁')} {name}", f"{cog.description}\n共 {len(cog.get_app_commands())} 個指令", False)
            for name, cog in self.bot.cogs.items()
            if cog.get_app_commands()
        ]
        embed = make_embed("📖 指令列表", "從下方選單選擇分類，查看詳細指令。", fields=fields)
        await interaction.response.send_message(embed=embed, view=HelpView(self.bot))

    @app_commands.command(name="ping", description="查看機器人延遲")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=make_embed("🏓 Pong！", f"WebSocket 延遲：**{round(self.bot.latency * 1000)} ms**")
        )

    @app_commands.command(name="botinfo", description="查看機器人資訊")
    async def botinfo(self, interaction: discord.Interaction):
        bot = self.bot
        embed = make_embed(
            f"🤖 {bot.user.name}",
            thumbnail=bot.user.display_avatar.url,
            fields=[
                ("延遲", f"{round(bot.latency * 1000)} ms"),
                ("啟動於", fmt(bot.start_time, "R")),
                ("伺服器數", len(bot.guilds)),
                ("使用者數", sum(g.member_count or 0 for g in bot.guilds)),
                ("Python", platform.python_version()),
                ("discord.py", discord.__version__),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="查看使用者資訊")
    @app_commands.describe(member="要查看的成員（預設是你自己）")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        roles = [r.mention for r in reversed(member.roles) if r != interaction.guild.default_role]
        embed = make_embed(
            str(member),
            color=member.colour.value or Colors.PRIMARY,
            thumbnail=member.display_avatar.url,
            fields=[
                ("ID", member.id),
                ("暱稱", member.display_name),
                ("機器人", "是" if member.bot else "否"),
                ("帳號建立", fmt(member.created_at, "R")),
                ("加入伺服器", fmt(member.joined_at, "R") if member.joined_at else "未知"),
                ("最高身分組", member.top_role.mention),
                (f"身分組（{len(roles)}）", " ".join(roles[:15]) or "無", False),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="查看伺服器資訊")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = make_embed(
            g.name,
            g.description,
            thumbnail=g.icon.url if g.icon else None,
            fields=[
                ("👑 擁有者", f"<@{g.owner_id}>"),
                ("🆔 ID", g.id),
                ("📅 建立時間", fmt(g.created_at, "D")),
                ("👥 成員", g.member_count),
                ("💬 頻道", f"文字 {len(g.text_channels)} / 語音 {len(g.voice_channels)}"),
                ("🏷️ 身分組", len(g.roles)),
                ("🚀 加成", f"Lv.{g.premium_tier}（{g.premium_subscription_count} 個）"),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="查看大頭貼")
    @app_commands.describe(member="要查看的成員（預設是你自己）")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.User] = None):
        member = member or interaction.user
        embed = make_embed(f"{member.display_name} 的大頭貼", image=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="embed", description="建立並發送自訂 Embed 訊息")
    @app_commands.describe(
        title="標題",
        description="內容（用 \\n 換行）",
        color="顏色，例如 #5865F2",
        image="圖片網址",
    )
    @require_perms(manage_messages=True)
    async def embed_cmd(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        color: Optional[str] = None,
        image: Optional[str] = None,
    ):
        try:
            colour = discord.Colour.from_str(color).value if color else Colors.PRIMARY
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("顏色格式錯誤，請使用像 `#5865F2` 的格式。"), ephemeral=True
            )
        embed = make_embed(title, description.replace("\\n", "\n"), color=colour, image=image, author=interaction.user)
        await interaction.response.send_message(embed=embed)

    # ───────── 擁有者專用（前綴指令）─────────
    @commands.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync_cmd(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        await ctx.send(f"已同步 {len(synced)} 個斜線指令。")

    @commands.command(name="reload", hidden=True)
    @commands.is_owner()
    async def reload_cmd(self, ctx: commands.Context, name: str):
        """!reload <模組名稱>，或 !reload all 重新載入全部已載入的模組。"""
        if name.lower() == "all":
            # 這個模組正在執行本指令，放到最後再重新載入
            targets = sorted(self.bot.extensions, key=lambda ext: ext == __name__)
        else:
            targets = [f"cogs.{name}"]

        ok, failed = [], []
        for ext in targets:
            short = ext.removeprefix("cogs.")
            try:
                await self.bot.reload_extension(ext)
                ok.append(f"`{short}`")
            except Exception as e:  # noqa: BLE001
                failed.append(f"`{short}`：{e}")

        lines = []
        if ok:
            lines.append("✅ 已重新載入：" + "、".join(ok))
        if failed:
            lines.append("❌ 失敗：\n" + "\n".join(failed))
        if ok:
            synced = await self.bot.tree.sync()  # 指令內容可能變了，順便同步
            lines.append(f"🔄 已同步 {len(synced)} 個斜線指令")
        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
