"""每日簽到：連續簽到加成、點數排行榜、連續天數獎勵身分組。所有設定都用指令調整，每個伺服器各自獨立。

  /checkin          簽到領點數            /checkin_top      點數排行榜
  /checkin_setup    設定點數、連續加成、換日時區（只會修改你填寫的項目）
  /checkin_reward   設定「連續簽到 N 天」獲得的身分組    /checkin_reward_off  取消某個連續獎勵
  /checkin_status   查看目前的設定
"""
import copy
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import Colors, JsonStore, error_embed, make_embed, require_perms, success_embed

fmt = discord.utils.format_dt

DEFAULTS = {
    "base": 100,        # 每次簽到的基本點數
    "bonus": 10,        # 連續簽到每多一天，額外加的點數
    "cap": 7,           # 連續加成最多累計幾天
    "offset": 8,        # 換日時區（UTC 偏移小時；台灣 = 8）
    "rewards": {},      # {"連續天數": 身分組ID}
}


def utc_text(offset: int) -> str:
    return f"UTC{offset:+d}" + "（台灣）" if offset == 8 else f"UTC{offset:+d}"


class CheckIn(commands.Cog, name="每日簽到"):
    """每日簽到、連續獎勵與排行榜"""

    emoji = "📅"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = JsonStore("data/checkin.json")  # {guild_id: {user_id: {last, streak, best, total, points}}}

    # ───────────────────── 設定存取 ─────────────────────
    def cfg(self, guild_id: int) -> dict:
        merged = copy.deepcopy(DEFAULTS)
        merged.update(self.bot.settings.get(guild_id, "checkin", {}) or {})
        return merged

    async def _update(self, guild_id: int, **values):
        stored = copy.deepcopy(self.bot.settings.get(guild_id, "checkin", {}) or {})
        stored.update(values)
        await self.bot.settings.set(guild_id, "checkin", stored)

    def _summary(self, guild: discord.Guild) -> list[tuple]:
        cfg = self.cfg(guild.id)
        rewards = "\n".join(
            f"連續 **{days}** 天 → {guild.get_role(rid).mention if guild.get_role(rid) else '（身分組已不存在）'}"
            for days, rid in sorted(cfg["rewards"].items(), key=lambda kv: int(kv[0]))
        ) or "無"
        return [
            ("基本點數", f"{cfg['base']} 點"),
            ("連續加成", f"每多 1 天 +{cfg['bonus']} 點（最多累計 {cfg['cap']} 天）"),
            ("換日時區", utc_text(cfg["offset"])),
            ("連續獎勵身分組", rewards, False),
        ]

    # ───────────────────── 簽到 ─────────────────────
    @app_commands.command(name="checkin", description="每日簽到領點數")
    @app_commands.guild_only()
    async def checkin(self, interaction: discord.Interaction):
        cfg = self.cfg(interaction.guild_id)
        tz = timezone(timedelta(hours=cfg["offset"]))
        today = datetime.now(tz).date()
        next_reset = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz)

        users = self.store.data.setdefault(str(interaction.guild_id), {})
        rec = users.setdefault(
            str(interaction.user.id), {"last": "", "streak": 0, "best": 0, "total": 0, "points": 0}
        )

        if rec["last"] == today.isoformat():
            embed = make_embed(
                "📅 你今天已經簽到過了",
                color=Colors.WARNING,
                fields=[
                    ("連續簽到", f"{rec['streak']} 天"),
                    ("累計點數", rec["points"]),
                    ("下次可簽到", fmt(next_reset, "R")),
                ],
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # 以下到儲存之前沒有 await，所以連按兩次不會重複簽到
        yesterday = (today - timedelta(days=1)).isoformat()
        rec["streak"] = rec["streak"] + 1 if rec["last"] == yesterday else 1
        gained = cfg["base"] + cfg["bonus"] * min(rec["streak"] - 1, cfg["cap"])
        rec["last"] = today.isoformat()
        rec["total"] += 1
        rec["points"] += gained
        rec["best"] = max(rec["best"], rec["streak"])
        await self.store.save()

        fields = [
            ("獲得點數", f"+{gained}"),
            ("累計點數", rec["points"]),
            ("連續簽到", f"{rec['streak']} 天（最高 {rec['best']} 天）"),
            ("累計簽到", f"{rec['total']} 次"),
        ]
        role = interaction.guild.get_role(cfg["rewards"].get(str(rec["streak"]), 0))
        if role:
            try:
                await interaction.user.add_roles(role, reason=f"連續簽到 {rec['streak']} 天")
                fields.append(("🎁 獎勵身分組", role.mention, False))
            except discord.HTTPException:
                pass

        embed = make_embed(
            "✅ 簽到成功！",
            color=Colors.SUCCESS,
            thumbnail=interaction.user.display_avatar.url,
            fields=fields + [("下次可簽到", fmt(next_reset, "R"))],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="checkin_top", description="簽到點數排行榜")
    @app_commands.guild_only()
    async def checkin_top(self, interaction: discord.Interaction):
        users = self.store.data.get(str(interaction.guild_id), {})
        ranking = sorted(
            ((uid, r) for uid, r in users.items() if r["points"] > 0),
            key=lambda item: item[1]["points"],
            reverse=True,
        )[:10]
        if not ranking:
            return await interaction.response.send_message("還沒有人簽到過，快來當第一個吧！", ephemeral=True)

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{r['points']}** 點（連續 {r['streak']} 天）"
            for i, (uid, r) in enumerate(ranking)
        ]
        await interaction.response.send_message(embed=make_embed("🏆 簽到排行榜", "\n".join(lines)))

    # ───────────────────── 設定指令 ─────────────────────
    @app_commands.command(name="checkin_status", description="查看每日簽到的目前設定")
    @require_perms(manage_guild=True)
    async def checkin_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=make_embed(
                "📅 每日簽到設定",
                "用 `/checkin_setup` 調整點數與時區，`/checkin_reward` 設定連續獎勵身分組。",
                fields=self._summary(interaction.guild),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="checkin_setup", description="設定簽到點數、連續加成與換日時區（只會修改你填寫的項目）")
    @app_commands.describe(
        base="每次簽到的基本點數",
        bonus="連續簽到每多一天，額外加幾點",
        cap="連續加成最多累計幾天",
        utc_offset="換日時區（UTC 偏移小時，台灣是 8）",
    )
    @app_commands.rename(base="基本點數", bonus="連續加成", cap="加成上限天數", utc_offset="時區")
    @require_perms(manage_guild=True)
    async def checkin_setup(
        self,
        interaction: discord.Interaction,
        base: Optional[app_commands.Range[int, 0, 100000]] = None,
        bonus: Optional[app_commands.Range[int, 0, 10000]] = None,
        cap: Optional[app_commands.Range[int, 0, 365]] = None,
        utc_offset: Optional[app_commands.Range[int, -12, 14]] = None,
    ):
        values = {
            k: v for k, v in {"base": base, "bonus": bonus, "cap": cap, "offset": utc_offset}.items() if v is not None
        }
        if not values:
            return await interaction.response.send_message(
                embed=error_embed(
                    "你沒有填寫任何要修改的項目。",
                    reason="這個指令只會修改你有填寫的欄位，全部留空就沒有東西可以改。",
                    fix="請至少填寫一個欄位；想看目前設定請用 `/checkin_status`。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, **values)
        await interaction.response.send_message(
            embed=make_embed("✅ 簽到設定已更新", color=Colors.SUCCESS, fields=self._summary(interaction.guild)),
            ephemeral=True,
        )

    @app_commands.command(name="checkin_reward", description="設定「連續簽到 N 天」可獲得的身分組")
    @app_commands.describe(days="連續簽到幾天（剛好達到這一天時給予）", role="要給予的身分組")
    @app_commands.rename(days="連續天數", role="身分組")
    @require_perms(manage_roles=True)
    async def checkin_reward(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650],
        role: discord.Role,
    ):
        if role.is_default() or not role.is_assignable():
            return await interaction.response.send_message(
                embed=error_embed(
                    f"我無法把 {role.mention} 給成員。",
                    reason="這個身分組是 @everyone、由整合服務管理，或者它的位置不低於機器人的最高身分組。",
                    fix="請到「伺服器設定 → 身分組」，把機器人的身分組拖到這個身分組的上方，或改選其他身分組。",
                ),
                ephemeral=True,
            )
        rewards = dict(self.cfg(interaction.guild_id)["rewards"])
        rewards[str(days)] = role.id
        await self._update(interaction.guild_id, rewards=rewards)
        await interaction.response.send_message(
            embed=success_embed(f"連續簽到 **{days}** 天的成員，會獲得 {role.mention}。"), ephemeral=True
        )

    @app_commands.command(name="checkin_reward_off", description="取消某個「連續簽到 N 天」的獎勵身分組")
    @app_commands.describe(days="要取消的連續天數")
    @app_commands.rename(days="連續天數")
    @require_perms(manage_roles=True)
    async def checkin_reward_off(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 3650]):
        rewards = dict(self.cfg(interaction.guild_id)["rewards"])
        if rewards.pop(str(days), None) is None:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"沒有設定「連續 {days} 天」的獎勵。",
                    reason="你輸入的天數沒有對應的獎勵身分組。",
                    fix="用 `/checkin_status` 查看目前有哪些天數設定了獎勵。",
                ),
                ephemeral=True,
            )
        await self._update(interaction.guild_id, rewards=rewards)
        await interaction.response.send_message(embed=success_embed(f"已取消「連續 {days} 天」的獎勵。"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CheckIn(bot))
