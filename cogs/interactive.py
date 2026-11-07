"""按鈕與選單：身分組選單面板、客服單（Ticket）、投票。
所有面板（身分組選單、客服單、投票）都是持久化的：機器人重啟後，舊訊息上的按鈕與選單仍可操作。
"""
import asyncio
import re
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import JsonStore, respond, Colors, error_embed, make_embed, require_perms, send_log, success_embed, warn_embed


# ═══════════════════════ 身分組選單 ═══════════════════════
class RolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.roles = [r for r in config.SELF_ROLES if r.get("role_id")]
        options = [
            discord.SelectOption(
                label=r["label"],
                value=str(r["role_id"]),
                emoji=r.get("emoji"),
                description=r.get("description"),
            )
            for r in self.roles
        ]
        self.select = discord.ui.Select(
            custom_id="rolepanel:select",
            placeholder="選擇你想要的身分組（可複選）…",
            min_values=0,
            max_values=max(len(options), 1),
            options=options,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        chosen = {int(v) for v in self.select.values}
        managed = {r["role_id"] for r in config.SELF_ROLES if r.get("role_id")}
        current = {r.id for r in member.roles}

        to_add = [r for r in (guild.get_role(i) for i in chosen - current) if r]
        to_remove = [r for r in (guild.get_role(i) for i in (managed & current) - chosen) if r]

        await interaction.response.defer(ephemeral=True)
        try:
            if to_add:
                await member.add_roles(*to_add, reason="身分組選單")
            if to_remove:
                await member.remove_roles(*to_remove, reason="身分組選單")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=error_embed("我沒有權限指派這些身分組，請管理員把我的身分組移到它們上方。"), ephemeral=True
            )

        added = " ".join(r.mention for r in to_add) or "無"
        removed = " ".join(r.mention for r in to_remove) or "無"
        await interaction.followup.send(
            embed=success_embed(f"身分組已更新\n➕ 新增：{added}\n➖ 移除：{removed}"), ephemeral=True
        )


# ═══════════════════════ 客服單 ═══════════════════════
class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="開啟客服單", emoji="📩", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, user = interaction.guild, interaction.user
        topic = f"ticket:{user.id}"

        existing = discord.utils.get(guild.text_channels, topic=topic)
        if existing:
            return await interaction.response.send_message(
                embed=warn_embed(f"你已經有一張客服單了：{existing.mention}"), ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        staff = guild.get_role(config.STAFF_ROLE_ID) if config.STAFF_ROLE_ID else None
        if staff:
            overwrites[staff] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        category = guild.get_channel(config.TICKET_CATEGORY_ID) if config.TICKET_CATEGORY_ID else None
        if not isinstance(category, discord.CategoryChannel):
            category = None

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{user.name}"[:90],
                category=category,
                topic=topic,
                overwrites=overwrites,
                reason=f"{user} 開啟客服單",
            )
        except discord.Forbidden:
            return await interaction.followup.send(embed=error_embed("我沒有建立頻道的權限。"), ephemeral=True)

        embed = make_embed(
            "📩 客服單已建立",
            f"{user.mention} 你好！請描述你遇到的問題，工作人員會盡快協助你。\n處理完畢後可按下方按鈕關閉。",
        )
        await channel.send(
            content=f"{user.mention} {staff.mention if staff else ''}".strip(),
            embed=embed,
            view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
        await interaction.followup.send(embed=success_embed(f"客服單已建立：{channel.mention}"), ephemeral=True)
        await send_log(
            guild,
            make_embed("📩 客服單開啟", color=Colors.SUCCESS, fields=[("使用者", user.mention), ("頻道", channel.mention)]),
        )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="關閉客服單", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel, user = interaction.channel, interaction.user
        topic = getattr(channel, "topic", None) or ""
        if not topic.startswith("ticket:"):
            return await interaction.response.send_message(embed=error_embed("這裡不是客服單頻道。"), ephemeral=True)

        try:
            owner_id = int(topic.split(":", 1)[1])
        except ValueError:
            owner_id = 0
        is_staff = bool(config.STAFF_ROLE_ID) and any(r.id == config.STAFF_ROLE_ID for r in user.roles)
        if user.id != owner_id and not (user.guild_permissions.manage_channels or is_staff):
            return await interaction.response.send_message(
                embed=error_embed("只有開單者或工作人員可以關閉客服單。"), ephemeral=True
            )

        await interaction.response.send_message(embed=warn_embed("客服單將在 5 秒後關閉…"))
        await asyncio.sleep(5)
        await send_log(
            interaction.guild,
            make_embed(
                "🔒 客服單關閉",
                color=Colors.ERROR,
                fields=[("頻道", f"`#{channel.name}`"), ("開單者", f"<@{owner_id}>"), ("關閉者", user.mention)],
            ),
        )
        await channel.delete(reason=f"客服單由 {user} 關閉")


# ═══════════════════════ 投票（持久化）═══════════════════════
# 投票資料存在 data/polls.json，按鈕用 DynamicItem（custom_id 內含投票 ID），
# 所以機器人重啟後，舊的投票訊息仍可繼續投票、結束。
POLL_KEEP_DAYS = 30  # 已結束的投票資料保留幾天


def poll_embed(poll: dict) -> discord.Embed:
    votes = poll["votes"]
    total = len(votes)
    blocks = []
    for i, option in enumerate(poll["options"]):
        count = sum(1 for v in votes.values() if v == i)
        pct = count / total * 100 if total else 0
        filled = round(pct / 10)
        blocks.append(f"**{i + 1}. {option}**\n{'█' * filled}{'░' * (10 - filled)} {count} 票（{pct:.0f}%）")
    end_line = "🏁 投票已結束" if poll["closed"] else f"⏰ 結束時間：<t:{int(poll['end'])}:R>"
    return make_embed(
        f"📊 {poll['question']}",
        "\n\n".join(blocks) + f"\n\n{end_line}",
        footer=f"共 {total} 人投票 · 由 {poll['author_name']} 發起" + (" · 已結束" if poll["closed"] else ""),
    )


class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<pid>[0-9a-f]{8}):(?P<idx>[0-4])"):
    def __init__(self, pid: str, idx: int, label: str = "投票", disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label=label[:70], style=discord.ButtonStyle.primary, custom_id=f"poll:{pid}:{idx}", disabled=disabled
            )
        )
        self.pid, self.idx = pid, idx

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["pid"], int(match["idx"]), label=item.label or "投票")

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("按鈕與選單").vote(interaction, self.pid, self.idx)


class PollEndButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll_end:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(
            discord.ui.Button(label="結束投票", emoji="🛑", style=discord.ButtonStyle.danger, custom_id=f"poll_end:{pid}")
        )
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("按鈕與選單").end_poll_click(interaction, self.pid)


def poll_view(pid: str, poll: dict, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for i, option in enumerate(poll["options"]):
        view.add_item(PollButton(pid, i, option, disabled=disabled))
    if not disabled:
        view.add_item(PollEndButton(pid))
    return view


# ═══════════════════════ Cog ═══════════════════════
class Interactive(commands.Cog, name="按鈕與選單"):
    """身分組選單、客服單與投票"""

    emoji = "🎛️"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_store = JsonStore("data/polls.json")  # {投票ID: {question, options, votes, end, closed, channel, message…}}

    async def cog_load(self):
        # 註冊持久化的按鈕與選單，讓重啟前發出的面板仍然有效
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketCloseView())
        if any(r.get("role_id") for r in config.SELF_ROLES):
            self.bot.add_view(RolePanelView())
        self.bot.add_dynamic_items(PollButton, PollEndButton)
        self.poll_loop.start()

    async def cog_unload(self):
        self.poll_loop.cancel()

    # ───────── 投票邏輯 ─────────
    @property
    def polls(self) -> dict:
        return self.poll_store.data

    async def _poll_missing(self, interaction: discord.Interaction):
        await respond(
            interaction,
            embed=error_embed(
                "找不到這個投票的資料。",
                reason="投票資料已過期被清除（結束超過 30 天），或 `data/polls.json` 被刪除了。",
                fix="請重新用 `/poll` 建立一個新的投票。",
            ),
            ephemeral=True,
        )

    async def vote(self, interaction: discord.Interaction, pid: str, idx: int):
        poll = self.polls.get(pid)
        if poll is None:
            return await self._poll_missing(interaction)
        if poll["closed"] or poll["end"] <= time.time():
            await self.close_poll(pid)
            return await respond(
                interaction,
                embed=error_embed("這個投票已經結束了。", reason="投票時間到了，或發起人已提前結束。", fix="如果還想投票，請請發起人重新建立。"),
                ephemeral=True,
            )
        poll["votes"][str(interaction.user.id)] = idx  # 可改票
        await self.poll_store.save()
        await interaction.response.edit_message(embed=poll_embed(poll))

    async def end_poll_click(self, interaction: discord.Interaction, pid: str):
        poll = self.polls.get(pid)
        if poll is None:
            return await self._poll_missing(interaction)
        if interaction.user.id != poll["author"] and not interaction.user.guild_permissions.manage_messages:
            return await respond(
                interaction,
                embed=error_embed(
                    "只有投票發起人或有「管理訊息」權限的人可以結束投票。",
                    reason=f"這個投票是 {poll['author_name']} 發起的。",
                    fix="請請發起人或管理員結束投票。",
                ),
                ephemeral=True,
            )
        await self.close_poll(pid, interaction=interaction)

    async def close_poll(self, pid: str, interaction: Optional[discord.Interaction] = None):
        poll = self.polls.get(pid)
        if poll is None or (poll["closed"] and interaction is None):
            return
        poll["closed"] = True
        poll.setdefault("closed_at", time.time())
        await self.poll_store.save()
        embed, view = poll_embed(poll), poll_view(pid, poll, disabled=True)
        if interaction is not None:
            return await interaction.response.edit_message(embed=embed, view=view)
        channel = self.bot.get_channel(poll["channel"])
        if channel and poll.get("message"):
            try:
                message = await channel.fetch_message(poll["message"])
                await message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass  # 訊息或頻道已被刪除

    @tasks.loop(minutes=1)
    async def poll_loop(self):
        """到時間的投票自動結束；太久以前結束的投票資料順便清掉。"""
        now = time.time()
        changed = False
        for pid, poll in list(self.polls.items()):
            if not poll["closed"] and poll["end"] <= now:
                await self.close_poll(pid)
            elif poll["closed"] and now - poll.get("closed_at", poll["end"]) > POLL_KEEP_DAYS * 86400:
                self.polls.pop(pid, None)
                changed = True
        if changed:
            await self.poll_store.save()

    @poll_loop.before_loop
    async def _before_poll_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="rolepanel", description="發送身分組選單面板")
    @require_perms(manage_roles=True)
    async def rolepanel(self, interaction: discord.Interaction):
        if not any(r.get("role_id") for r in config.SELF_ROLES):
            return await interaction.response.send_message(
                embed=error_embed("請先在 config.py 的 `SELF_ROLES` 填入身分組，並重新啟動機器人。"), ephemeral=True
            )
        embed = make_embed("🏷️ 身分組選單", "從下方選單選擇你想要的身分組，取消勾選即可移除。")
        await interaction.response.send_message(embed=embed, view=RolePanelView())

    @app_commands.command(name="ticketpanel", description="發送客服單面板")
    @require_perms(manage_channels=True)
    async def ticketpanel(self, interaction: discord.Interaction):
        embed = make_embed("🎫 客服中心", "需要協助嗎？按下下方按鈕開啟專屬的私人客服單。")
        await interaction.response.send_message(embed=embed, view=TicketPanelView())

    @app_commands.command(name="poll", description="建立投票（按鈕；重啟後仍可繼續投票）")
    @app_commands.describe(
        question="投票主題",
        options="選項，用逗號或 | 分隔（2-5 個），例如：披薩, 漢堡, 壽司",
        hours="投票持續幾小時（預設 24，最長 168）",
    )
    @app_commands.guild_only()
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 200],
        options: str,
        hours: app_commands.Range[int, 1, 168] = 24,
    ):
        choices = [o.strip() for o in re.split(r"[,，|]", options) if o.strip()]
        if not 2 <= len(choices) <= 5:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"選項數量不對（你給了 {len(choices)} 個）。",
                    reason="投票需要 2 到 5 個選項。",
                    fix="請用逗號或 | 分隔選項，例如：`披薩, 漢堡, 壽司`。",
                ),
                ephemeral=True,
            )
        pid = uuid.uuid4().hex[:8]
        poll = {
            "question": question,
            "options": [c[:70] for c in choices],
            "author": interaction.user.id,
            "author_name": interaction.user.display_name,
            "votes": {},
            "closed": False,
            "end": time.time() + hours * 3600,
            "channel": interaction.channel_id,
            "message": None,
        }
        self.polls[pid] = poll
        await interaction.response.send_message(embed=poll_embed(poll), view=poll_view(pid, poll))
        poll["message"] = (await interaction.original_response()).id
        await self.poll_store.save()


async def setup(bot: commands.Bot):
    await bot.add_cog(Interactive(bot))
