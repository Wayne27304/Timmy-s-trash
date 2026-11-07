"""集中處理錯誤：把例外轉成「發生什麼事 → 可能原因 → 解決方式 → 技術細節」的詳細 Embed。

斜線指令、前綴指令、按鈕/選單、表單的錯誤都會經過這裡（見 bot.py）。
"""
import asyncio
import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from utils import error_embed, perm_text, truncate

log = logging.getLogger("errors")

# Discord API 常見錯誤代碼 → 白話說明
DISCORD_CODES = {
    10003: "找不到頻道（可能已被刪除）",
    10004: "找不到伺服器",
    10007: "找不到成員（對方可能已離開伺服器）",
    10008: "找不到訊息（可能已被刪除）",
    10011: "找不到身分組（可能已被刪除）",
    10062: "互動已逾時：機器人沒在 3 秒內回應（可能是機器人太忙、網路延遲或剛重啟）",
    30005: "身分組數量已達伺服器上限（250 個）",
    30013: "頻道數量已達伺服器上限（500 個）",
    40060: "這個互動已經回應過了",
    50001: "機器人沒有權限存取這個頻道或項目",
    50007: "無法私訊對方（對方關閉了私訊，或封鎖了機器人）",
    50013: "機器人缺少權限，或機器人的身分組位置不夠高",
    50035: "送給 Discord 的資料格式不正確（例如文字太長、網址無效）",
}

FORBIDDEN_FIX = (
    "1. 到「伺服器設定 → 身分組」，把**機器人的身分組拖到更高的位置**（要高於被操作的對象與身分組）\n"
    "2. 確認機器人的身分組已開啟這個操作需要的權限（建議授予「管理員」）\n"
    "3. 檢查該頻道的權限覆寫有沒有禁止機器人"
)


def new_error_id() -> str:
    return uuid.uuid4().hex[:8].upper()


def _http_detail(error: discord.HTTPException) -> str:
    return f"HTTP {error.status}｜Discord 代碼 {error.code}\n{error.text}"


def build_error_embed(error: Exception, where: str = "") -> discord.Embed:
    """依例外類型產生詳細的錯誤 Embed。where 是出錯的指令，例如 "/ban"。"""
    original = getattr(error, "original", None) or error
    target = f"`{where}`" if where else "這個操作"

    # ───────── 權限 / 檢查類 ─────────
    if isinstance(error, (app_commands.MissingPermissions, commands.MissingPermissions)):
        return error_embed(
            f"你沒有權限使用 {target}。",
            reason=f"你缺少以下權限：{perm_text(error.missing_permissions)}",
            fix="請向伺服器管理員索取權限，或請有權限的成員代為執行。",
        )
    if isinstance(error, (app_commands.BotMissingPermissions, commands.BotMissingPermissions)):
        return error_embed(
            f"我缺少執行 {target} 所需的權限。",
            reason=f"機器人缺少以下權限：{perm_text(error.missing_permissions)}",
            fix="到「伺服器設定 → 身分組」替機器人的身分組開啟這些權限，或在該頻道的權限設定中允許機器人。",
        )
    if isinstance(error, (app_commands.CommandOnCooldown, commands.CommandOnCooldown)):
        return error_embed(
            f"{target} 還在冷卻中。",
            reason="同一個指令短時間內不能重複使用，避免洗版與過度消耗資源。",
            fix=f"請等待 **{error.retry_after:.1f} 秒** 後再試。",
        )
    if isinstance(error, (app_commands.NoPrivateMessage, commands.NoPrivateMessage)):
        return error_embed(
            f"{target} 只能在伺服器中使用。",
            reason="你是在私訊、群組或機器人無法判斷伺服器的地方使用的。",
            fix="請回到伺服器的文字頻道再使用這個指令。",
        )
    if isinstance(error, (app_commands.CheckFailure, commands.CheckFailure)):
        return error_embed(
            str(error) or f"你目前無法使用 {target}。",
            reason="這個指令有使用條件（例如指定身分組、所在頻道、開發者限定，或你被列入黑名單），你目前不符合。",
            fix="請確認你符合使用條件；如果覺得是誤判，請聯絡伺服器管理員。",
        )

    # ───────── 參數 / 同步類 ─────────
    if isinstance(error, app_commands.TransformerError):
        return error_embed(
            f"{target} 的參數格式不正確。",
            reason=f"無法把你輸入的 `{truncate(str(error.value), 80)}` 轉換成指令需要的類型。",
            fix="請重新輸入指令，並使用 Discord 跳出的選項，或依欄位說明填寫。",
        )
    if isinstance(error, app_commands.CommandSignatureMismatch):
        return error_embed(
            f"{target} 的版本與 Discord 上的不一致。",
            reason="機器人剛更新過指令，但你的 Discord 還在使用舊的指令資料。",
            fix="請稍等 1～2 分鐘，並重新整理 Discord（電腦按 Ctrl+R；手機重開 App）。若一直如此，請開發者執行 `!sync`。",
        )
    if isinstance(error, commands.MissingRequiredArgument):
        return error_embed(
            f"{target} 缺少必要的參數。",
            reason=f"缺少參數：`{error.param.name}`",
            fix="請補上缺少的參數後再試一次。",
        )
    if isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
        return error_embed(
            f"{target} 的參數格式不正確。",
            reason=str(error) or "參數類型不符。",
            fix="請檢查參數是否正確（例如 ID 要是數字、成員要能被找到）。",
        )

    # ───────── Discord API 錯誤 ─────────
    if isinstance(original, discord.Forbidden):
        code_text = DISCORD_CODES.get(original.code)
        reason = (
            (f"Discord 回報：**{code_text}**\n\n" if code_text else "")
            + "常見原因：\n• 機器人缺少這個操作的權限\n• 機器人的身分組位置低於操作對象（身分組階級不足）\n"
            "• 頻道權限覆寫禁止了機器人\n• 對象是伺服器擁有者（任何人都無法被操作）\n• 對方關閉了私訊（若要傳送私訊）"
        )
        return error_embed(
            f"Discord 拒絕了 {target} 要求的操作（403）。",
            reason=reason, fix=FORBIDDEN_FIX, detail=_http_detail(original),
        )
    if isinstance(original, discord.NotFound):
        return error_embed(
            f"找不到 {target} 要操作的對象（404）。",
            reason=f"Discord 回報：**{DISCORD_CODES.get(original.code, '找不到目標')}**\n"
                   "目標可能已被刪除、對方已離開伺服器，或這個互動已經過期。",
            fix="請重新執行一次指令；如果是按鈕或選單，請改用最新的訊息操作。",
            detail=_http_detail(original),
        )
    if isinstance(original, discord.HTTPException):
        if original.status == 429:
            reason, fix = "Discord 限制了請求頻率，短時間內操作太多次。", "請等幾十秒後再試，並避免連續快速操作。"
        elif original.status >= 500:
            reason, fix = "Discord 伺服器暫時發生問題，不是你或機器人的錯。", "請稍後再試；可到 discordstatus.com 查看 Discord 狀態。"
        else:
            reason = f"Discord 回報：**{DISCORD_CODES.get(original.code, '請求被拒絕')}**"
            fix = "請檢查輸入的內容（長度、格式、網址）是否正確後再試一次。"
        return error_embed(
            f"Discord 無法完成 {target}（HTTP {original.status}）。",
            reason=reason, fix=fix, detail=_http_detail(original),
        )
    if isinstance(original, (asyncio.TimeoutError, TimeoutError)):
        return error_embed(
            f"{target} 執行逾時。",
            reason="機器人等待外部服務（Discord、AI 或音樂節點）回應太久。",
            fix="請稍後再試；如果一直發生，請通知開發者檢查網路或節點狀態。",
        )

    # ───────── 未預期的錯誤 ─────────
    error_id = new_error_id()
    log.error("[%s] %s 發生未預期的錯誤", error_id, where or "（未知）", exc_info=original)
    return error_embed(
        f"執行 {target} 時發生了未預期的錯誤。",
        title="❌ 發生未預期的錯誤",
        reason="通常是機器人程式或外部服務（Discord、AI、音樂節點）暫時出問題，不是你的操作錯誤。",
        fix="請稍後再試一次；如果持續發生，請把下方的錯誤代碼與技術細節提供給開發者。",
        detail=f"{type(original).__name__}: {truncate(str(original) or '（無訊息）', 300)}",
        error_id=error_id,
    )
