"""數數字遊戲：大家依序從 1 開始往上數。
規則：只能接下一個數字；同一人不能連續數兩次（可在 config 調整）；數錯就從 1 重來。
訊息開頭不是數字的內容視為聊天，不影響遊戲。
"""
import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import Colors, JsonStore, error_embed, make_embed, require_perms, success_embed


class Counting(commands.Cog, name="數數字"):
    """大家一起從 1 開始往上數"""

    emoji = "🔢"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = JsonStore("data/counting.json")  # {channel_id: {guild, current, last_user, last_msg, high}}
        self.lock = asyncio.Lock()

    # ───────────────────── 遊戲邏輯 ─────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        state = self.store.data.get(str(message.channel.id))
        if not state or self.bot.is_blacklisted(message.guild.id, message.author.id):
            return

        parts = message.content.split(maxsplit=1)
        first = parts[0] if parts else ""
        if not (first.isascii() and first.isdigit() and len(first) <= 18):
            return  # 不是數字開頭 → 當作聊天
        number = int(first)

        async with self.lock:
            expected = state["current"] + 1
            if message.author.id == state["last_user"] and not config.COUNTING_ALLOW_SAME_USER:
                return await self._fail(message, state, "不能連續數兩次")
            if number != expected:
                return await self._fail(message, state, f"下一個數字應該是 **{expected}**，不是 {number}")

            state.update(current=number, last_user=message.author.id, last_msg=message.id)
            new_high = number > state["high"]
            if new_high:
                state["high"] = number
            await self.store.save()

        await self._react(message, "🏆" if new_high and number >= 10 else "✅")

    async def _fail(self, message: discord.Message, state: dict, reason: str):
        broke_at = state["current"]
        state.update(current=0, last_user=0, last_msg=0)
        await self.store.save()
        await self._react(message, "❌")
        embed = make_embed(
            "💥 數數字中斷！",
            f"{message.author.mention} {reason}。\n連續紀錄停在 **{broke_at}**，重新從 **1** 開始！",
            color=Colors.ERROR,
            fields=[("歷史最高", state["high"])],
        )
        await message.channel.send(embed=embed)

    @staticmethod
    async def _react(message: discord.Message, emoji: str):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    # 偷偷刪除 / 編輯剛數的數字 → 提醒大家目前的數字
    async def _tamper(self, message: discord.Message, verb: str):
        state = self.store.data.get(str(message.channel.id))
        if not state or message.author.bot or message.id != state.get("last_msg"):
            return
        await message.channel.send(
            f"{message.author.mention} {verb}了他的數字！目前的數字是 **{state['current']}**，"
            f"下一個是 **{state['current'] + 1}**。"
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild:
            await self._tamper(message, "刪除")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild and before.content != after.content:
            await self._tamper(after, "編輯")

    # ───────────────────── 指令 ─────────────────────
    @app_commands.command(name="counting_setup", description="把頻道設為數數字頻道")
    @app_commands.describe(channel="要用來數數字的文字頻道")
    @require_perms(manage_channels=True)
    async def counting_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        old = self.store.data.get(str(channel.id), {})
        self.store.data[str(channel.id)] = {
            "guild": interaction.guild_id, "current": 0, "last_user": 0, "last_msg": 0,
            "high": old.get("high", 0),
        }
        await self.store.save()
        rules = (
            "從 **1** 開始，大家輪流往上數！\n"
            "• 同一個人不能連續數兩次\n• 數錯就會從 1 重來\n• 開頭不是數字的訊息視為聊天"
        )
        if config.COUNTING_ALLOW_SAME_USER:
            rules = rules.replace("• 同一個人不能連續數兩次\n", "")
        await channel.send(embed=make_embed("🔢 數數字開始！", rules))
        await interaction.response.send_message(
            embed=success_embed(f"已將 {channel.mention} 設為數數字頻道。"), ephemeral=True
        )

    @app_commands.command(name="counting_off", description="取消數數字頻道")
    @app_commands.describe(channel="要取消的頻道")
    @require_perms(manage_channels=True)
    async def counting_off(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if self.store.data.pop(str(channel.id), None) is None:
            return await interaction.response.send_message(embed=error_embed("這個頻道不是數數字頻道。"), ephemeral=True)
        await self.store.save()
        await interaction.response.send_message(embed=success_embed(f"已取消 {channel.mention} 的數數字。"))

    @app_commands.command(name="counting_stats", description="查看數數字紀錄")
    @app_commands.describe(channel="要查看的頻道（預設：目前頻道）")
    @app_commands.guild_only()
    async def counting_stats(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        state = self.store.data.get(str(channel.id))
        if not state:
            return await interaction.response.send_message(embed=error_embed("這個頻道不是數數字頻道。"), ephemeral=True)
        last = f"<@{state['last_user']}>" if state["last_user"] else "—"
        embed = make_embed(
            "🔢 數數字紀錄",
            fields=[
                ("頻道", channel.mention),
                ("目前數字", state["current"]),
                ("下一個", state["current"] + 1),
                ("歷史最高", state["high"]),
                ("最後一位", last),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="counting_reset", description="重設數數字（從 1 重新開始）")
    @app_commands.describe(channel="要重設的頻道（預設：目前頻道）")
    @require_perms(manage_channels=True)
    async def counting_reset(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        state = self.store.data.get(str(channel.id))
        if not state:
            return await interaction.response.send_message(embed=error_embed("這個頻道不是數數字頻道。"), ephemeral=True)
        state.update(current=0, last_user=0, last_msg=0)
        await self.store.save()
        await interaction.response.send_message(embed=success_embed(f"{channel.mention} 已重設，從 **1** 開始。"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
