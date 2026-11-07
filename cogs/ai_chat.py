"""AI 聊天（Groq）
使用方式：
  1. @機器人 後面接問題
  2. 在 config.AI_CHANNEL_IDS 內的頻道直接說話（不用 @）
  3. /ask 問題
每個頻道各自保留最近幾輪對話記憶（存在記憶體，重啟後清空）。
需要在 config.py 填入 GROQ_API_KEY（https://console.groq.com/keys），模型用 config.AI_MODEL 設定。
"""
import logging
import re
import time
from collections import defaultdict, deque
from typing import Optional

import discord
import groq
from discord import app_commands
from discord.ext import commands

import config
from utils import error_embed, make_embed, split_message, success_embed, truncate

log = logging.getLogger("ai_chat")
NO_MENTIONS = discord.AllowedMentions.none()  # 避免 AI 輸出的 @everyone / @身分組 真的被觸發


class AIChat(commands.Cog, name="AI 聊天"):
    """與 AI 對話（Groq）：@我、指定頻道，或使用 /ask"""

    emoji = "🤖"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = groq.AsyncGroq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None
        self.history = defaultdict(lambda: deque(maxlen=config.AI_HISTORY_TURNS * 2))  # {channel_id: [訊息]}
        self.last_used: dict[int, float] = {}  # {user_id: 上次使用時間}

    async def cog_unload(self):
        if self.client:
            await self.client.close()

    # ───────────────────── 核心 ─────────────────────
    async def respond(self, channel_id: int, user: discord.abc.User, text: str) -> tuple[bool, discord.Embed | str]:
        """回傳 (是否成功, 回覆文字)。失敗時第二個值是詳細的錯誤 Embed。"""
        if not self.client:
            return False, error_embed(
                "AI 聊天尚未啟用。",
                reason="`config.py` 裡的 `GROQ_API_KEY` 是空的，機器人沒有金鑰可以呼叫 Groq。",
                fix="請管理員到 https://console.groq.com/keys 建立 API Key（有免費額度），填入 `GROQ_API_KEY` 後重新啟動機器人。",
            )

        left = config.AI_COOLDOWN - (time.monotonic() - self.last_used.get(user.id, 0))
        if left > 0:
            return False, error_embed(
                f"你問得太快了，請等 **{left:.0f} 秒** 再問我 🙏",
                title="⏳ 冷卻中",
                reason=f"每位使用者每 {config.AI_COOLDOWN} 秒只能問一次，避免洗版與用光 API 額度。",
            )
        self.last_used[user.id] = time.monotonic()

        history = self.history[channel_id]
        history.append({"role": "user", "content": f"{user.display_name}: {text}"})
        messages = list(history)
        while messages and messages[0]["role"] != "user":  # 對話必須從 user 開始
            messages.pop(0)

        try:
            resp = await self.client.chat.completions.create(
                model=config.AI_MODEL,
                messages=[{"role": "system", "content": config.AI_SYSTEM_PROMPT}, *messages],
                max_tokens=config.AI_MAX_TOKENS,
                temperature=config.AI_TEMPERATURE,
            )
        except Exception as e:  # noqa: BLE001
            history.pop()
            return False, self._error_embed(e)

        choice = resp.choices[0]
        reply = (choice.message.content or "").strip() or "（沒有產生回覆）"
        if choice.finish_reason == "length":
            reply += "\n\n…（回覆太長被截斷了）"
        history.append({"role": "assistant", "content": reply})
        return True, reply

    @staticmethod
    def _error_embed(e: Exception) -> discord.Embed:
        """把 Groq 的錯誤轉成詳細、好懂的訊息。"""
        detail = f"{type(e).__name__}: {truncate(str(e), 300)}"
        if isinstance(e, groq.AuthenticationError):
            return error_embed(
                "Groq 拒絕了機器人的 API Key。",
                reason="`GROQ_API_KEY` 填錯、已被刪除，或帳號被停用。",
                fix="請管理員到 https://console.groq.com/keys 確認或重新建立 Key，更新 `config.py` 後重新啟動。",
                detail=detail,
            )
        if isinstance(e, groq.RateLimitError):
            return error_embed(
                "AI 目前太忙了（超過 Groq 的使用額度）。",
                reason="短時間內請求太多，或今天的免費額度（每分鐘 / 每日請求數、token 數）用完了。",
                fix="請等一兩分鐘再試；若常發生，可把 `AI_MODEL` 換成較輕量的 `llama-3.1-8b-instant`，或升級 Groq 方案。",
                detail=detail,
            )
        if isinstance(e, groq.NotFoundError):
            return error_embed(
                f"找不到模型 `{config.AI_MODEL}`。",
                reason="模型名稱拼錯，或 Groq 已經下架這個模型。",
                fix="請到 https://console.groq.com/docs/models 查看目前可用的模型，更新 `config.py` 的 `AI_MODEL`。",
                detail=detail,
            )
        if isinstance(e, groq.BadRequestError):
            return error_embed(
                "Groq 無法處理這次的請求。",
                reason="模型可能已停用或不支援這種輸入，或對話內容超過模型長度限制。",
                fix="可以先用 `/ai_reset` 清除這個頻道的對話記憶再問一次；若仍失敗，請管理員檢查 `AI_MODEL`。",
                detail=detail,
            )
        if isinstance(e, groq.APIConnectionError):
            return error_embed(
                "連不上 Groq 伺服器。",
                reason="機器人的網路無法連到 api.groq.com（網路中斷、被防火牆擋住，或 Groq 暫時離線）。",
                fix="請稍後再試；若持續發生，請管理員檢查主機網路，或到 status.groq.com 查看服務狀態。",
                detail=detail,
            )
        if isinstance(e, groq.APIStatusError) and getattr(e, "status_code", 0) >= 500:
            return error_embed(
                "Groq 伺服器暫時發生問題。",
                reason="這是 Groq 那邊的錯誤（5xx），不是你或機器人的問題。",
                fix="請稍後再試。",
                detail=detail,
            )
        log.exception("Groq 未預期的錯誤", exc_info=e)
        return error_embed(
            "AI 暫時無法回應。",
            reason="呼叫 Groq 時發生未預期的錯誤。",
            fix="請稍後再試；若持續發生，請把下方技術細節提供給管理員。",
            detail=detail,
        )

    # ───────────────────── @ 機器人 / 指定頻道 ─────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or message.content.startswith(config.PREFIX):
            return
        if self.bot.is_blacklisted(message.guild.id, message.author.id):
            return

        dev = self.bot.get_cog("開發者")
        if dev and await dev.is_dev_command(message):
            return  # 「@機器人 狀態」等開發者指令由 developer.py 處理

        mentioned = self.bot.user in message.mentions and not message.mention_everyone
        if not (mentioned or message.channel.id in config.AI_CHANNEL_IDS):
            return

        text = re.sub(rf"<@!?{self.bot.user.id}>", "", message.content).strip()
        if not text:
            return await message.reply("嗨！有什麼我可以幫忙的嗎？ 😄", mention_author=False)

        async with message.channel.typing():
            ok, reply = await self.respond(message.channel.id, message.author, text)

        if not ok:
            return await message.reply(embed=reply, mention_author=False)
        for i, chunk in enumerate(split_message(reply)):
            if i == 0:
                await message.reply(chunk, mention_author=False, allowed_mentions=NO_MENTIONS)
            else:
                await message.channel.send(chunk, allowed_mentions=NO_MENTIONS)

    # ───────────────────── 斜線指令 ─────────────────────
    @app_commands.command(name="ask", description="向 AI 提問")
    @app_commands.describe(question="你想問的問題")
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 1500]):
        await interaction.response.defer(thinking=True)
        ok, reply = await self.respond(interaction.channel_id, interaction.user, question)
        if not ok:
            return await interaction.followup.send(embed=reply, ephemeral=True)

        for i, chunk in enumerate(split_message(reply, 4000)):
            embed = make_embed(f"💬 {truncate(question, 240)}" if i == 0 else None, chunk)
            await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="ai_reset", description="清除這個頻道的 AI 對話記憶")
    async def ai_reset(self, interaction: discord.Interaction):
        self.history.pop(interaction.channel_id, None)
        await interaction.response.send_message(embed=success_embed("已清除這個頻道的對話記憶。"))


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
