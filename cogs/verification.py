"""驗證系統與「加入自動給身分組」。

典型用法：
  A. 新成員加入先拿「未驗證」→ 驗證後【移除】它
       /autorole_set role:未驗證
       /verify_setup remove_role:未驗證
  B. 驗證後【新增】「已驗證」身分組
       /verify_setup add_role:已驗證
  C. 兩者可同時設定（先移除舊的、同時新增新的）
設定好之後用 /verify_panel 在想要的頻道發送驗證面板。
"""
import logging
import random
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import Colors, error_embed, make_embed, require_perms, send_log, success_embed

log = logging.getLogger("verification")


# ───────────────────────── 驗證流程 ─────────────────────────
def pending_roles(guild: discord.Guild, member: discord.Member, cfg: dict):
    """回傳 (要新增的身分組, 要移除的身分組)；都沒有代表已經驗證過。"""
    add = guild.get_role(cfg.get("add_role") or 0)
    remove = guild.get_role(cfg.get("remove_role") or 0)
    return (
        add if add and add not in member.roles else None,
        remove if remove and remove in member.roles else None,
    )


async def apply_roles(interaction: discord.Interaction, to_add, to_remove):
    if not to_add and not to_remove:
        return await interaction.response.send_message(embed=success_embed("你已經通過驗證了！"), ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    try:
        if to_add:
            await member.add_roles(to_add, reason="驗證通過")
        if to_remove:
            await member.remove_roles(to_remove, reason="驗證通過")
    except discord.Forbidden:
        return await interaction.followup.send(
            embed=error_embed("我無法調整你的身分組，請通知管理員檢查我的權限與身分組位置。"), ephemeral=True
        )

    lines = ["🎉 驗證成功！"]
    if to_add:
        lines.append(f"➕ 已取得 {to_add.mention}")
    if to_remove:
        lines.append(f"➖ 已移除 {to_remove.mention}")
    await interaction.followup.send(embed=success_embed("\n".join(lines)), ephemeral=True)
    await send_log(
        interaction.guild,
        make_embed("✅ 成員通過驗證", color=Colors.SUCCESS, author=member),
    )


class CaptchaModal(discord.ui.Modal, title="人機驗證"):
    def __init__(self):
        super().__init__(timeout=180)
        self.a, self.b = random.randint(2, 9), random.randint(2, 9)
        self.answer = discord.ui.TextInput(
            label=f"請回答：{self.a} + {self.b} = ?", min_length=1, max_length=3, placeholder="輸入數字"
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        if self.answer.value.strip() != str(self.a + self.b):
            return await interaction.response.send_message(
                embed=error_embed("答案不正確，請再按一次驗證按鈕重新作答。"), ephemeral=True
            )
        cfg = interaction.client.settings.get(interaction.guild_id, "verify") or {}
        to_add, to_remove = pending_roles(interaction.guild, interaction.user, cfg)
        await apply_roles(interaction, to_add, to_remove)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="點我驗證", emoji="✅", style=discord.ButtonStyle.success, custom_id="verify:button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = interaction.client.settings.get(interaction.guild_id, "verify")
        if not cfg:
            return await interaction.response.send_message(embed=error_embed("管理員尚未設定驗證系統。"), ephemeral=True)

        to_add, to_remove = pending_roles(interaction.guild, interaction.user, cfg)
        if not to_add and not to_remove:
            return await interaction.response.send_message(embed=success_embed("你已經通過驗證了！"), ephemeral=True)
        if cfg.get("method") == "captcha":
            return await interaction.response.send_modal(CaptchaModal())
        await apply_roles(interaction, to_add, to_remove)


# ───────────────────────── Cog ─────────────────────────
class Verification(commands.Cog, name="驗證與身分組"):
    """驗證系統、加入時自動給身分組"""

    emoji = "✅"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyView())  # 重啟後面板仍可使用

    @staticmethod
    def role_error(interaction: discord.Interaction, role: discord.Role) -> Optional[str]:
        guild = interaction.guild
        if role.is_default():
            return "不能使用 @everyone。"
        if role.managed:
            return "這個身分組由機器人/整合服務管理，無法指派。"
        if role >= guild.me.top_role:
            return f"我的最高身分組必須高於 {role.mention}，請先調整身分組順序。"
        if interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
            return "該身分組不低於你的最高身分組。"
        return None

    # ───────────────────── 加入自動給身分組 ─────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if self.bot.is_blacklisted(guild.id, member.id):
            return
        cfg = self.bot.settings.get(guild.id, "autorole", {})
        role_id = cfg.get("bot" if member.bot else "member")
        role = guild.get_role(role_id) if role_id else None
        if not role:
            return
        try:
            await member.add_roles(role, reason="加入自動身分組")
        except discord.HTTPException:
            log.warning("無法給予 %s 自動身分組 %s（權限或身分組位置不足）", member, role.name)

    @app_commands.command(name="autorole_set", description="設定成員加入時自動取得的身分組")
    @app_commands.describe(role="要自動給予的身分組", target="給誰（預設：一般成員）")
    @require_perms(manage_roles=True)
    async def autorole_set(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        target: Literal["成員", "機器人"] = "成員",
    ):
        if err := self.role_error(interaction, role):
            return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)
        cfg = dict(self.bot.settings.get(interaction.guild_id, "autorole", {}))
        cfg["bot" if target == "機器人" else "member"] = role.id
        await self.bot.settings.set(interaction.guild_id, "autorole", cfg)
        await interaction.response.send_message(
            embed=success_embed(f"之後加入的{target}會自動取得 {role.mention}。")
        )

    @app_commands.command(name="autorole_off", description="關閉加入自動身分組")
    @app_commands.describe(target="要關閉哪一種")
    @require_perms(manage_roles=True)
    async def autorole_off(self, interaction: discord.Interaction, target: Literal["成員", "機器人", "全部"] = "全部"):
        cfg = dict(self.bot.settings.get(interaction.guild_id, "autorole", {}))
        for key, label in (("member", "成員"), ("bot", "機器人")):
            if target in (label, "全部"):
                cfg.pop(key, None)
        await self.bot.settings.set(interaction.guild_id, "autorole", cfg)
        await interaction.response.send_message(embed=success_embed(f"已關閉「{target}」的自動身分組。"))

    @app_commands.command(name="autorole_show", description="查看目前的自動身分組設定")
    @require_perms(manage_roles=True)
    async def autorole_show(self, interaction: discord.Interaction):
        guild = interaction.guild
        cfg = self.bot.settings.get(guild.id, "autorole", {})
        member_id = cfg.get("member")
        bot_id = cfg.get("bot")
        fmt = lambda rid: (guild.get_role(rid).mention if rid and guild.get_role(rid) else "（未設定）")  # noqa: E731
        embed = make_embed("🏷️ 自動身分組", fields=[("一般成員", fmt(member_id)), ("機器人", fmt(bot_id))])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────── 驗證系統 ─────────────────────
    @app_commands.command(name="verify_setup", description="設定驗證系統（驗證後新增和／或移除身分組）")
    @app_commands.describe(
        add_role="驗證通過後【新增】的身分組（例如：已驗證）",
        remove_role="驗證通過後【移除】的身分組（例如：未驗證）",
        method="驗證方式",
    )
    @require_perms(manage_roles=True)
    async def verify_setup(
        self,
        interaction: discord.Interaction,
        add_role: Optional[discord.Role] = None,
        remove_role: Optional[discord.Role] = None,
        method: Literal["按鈕", "算術題"] = "按鈕",
    ):
        if not add_role and not remove_role:
            return await interaction.response.send_message(
                embed=error_embed("請至少設定 `add_role`（新增）或 `remove_role`（移除）其中一個。"), ephemeral=True
            )
        for role in (add_role, remove_role):
            if role and (err := self.role_error(interaction, role)):
                return await interaction.response.send_message(embed=error_embed(err), ephemeral=True)

        await self.bot.settings.set(
            interaction.guild_id,
            "verify",
            {
                "add_role": add_role.id if add_role else 0,
                "remove_role": remove_role.id if remove_role else 0,
                "method": "captcha" if method == "算術題" else "button",
            },
        )
        lines = []
        if add_role:
            lines.append(f"➕ 驗證通過後**新增**：{add_role.mention}")
        if remove_role:
            lines.append(f"➖ 驗證通過後**移除**：{remove_role.mention}")
        lines.append(f"🧩 驗證方式：{method}")
        lines.append("\n接著在想放面板的頻道使用 `/verify_panel`。")
        await interaction.response.send_message(embed=success_embed("驗證系統已設定\n" + "\n".join(lines)))

    @app_commands.command(name="verify_panel", description="在目前頻道發送驗證面板")
    @app_commands.describe(title="面板標題", description="面板說明")
    @require_perms(manage_roles=True)
    async def verify_panel(
        self,
        interaction: discord.Interaction,
        title: str = "✅ 伺服器驗證",
        description: str = "按下下方按鈕完成驗證，即可解鎖更多頻道。",
    ):
        if not self.bot.settings.get(interaction.guild_id, "verify"):
            return await interaction.response.send_message(
                embed=error_embed("請先使用 `/verify_setup` 設定驗證系統。"), ephemeral=True
            )
        embed = make_embed(title, description.replace("\\n", "\n"))
        await interaction.response.send_message(embed=embed, view=VerifyView())

    @app_commands.command(name="verify_off", description="關閉驗證系統（已發送的面板會顯示尚未設定）")
    @require_perms(manage_roles=True)
    async def verify_off(self, interaction: discord.Interaction):
        await self.bot.settings.remove(interaction.guild_id, "verify")
        await interaction.response.send_message(embed=success_embed("已關閉驗證系統。"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
