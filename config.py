"""
機器人設定檔
⚠️ 內含機密，請勿上傳到公開的 GitHub（.gitignore 已排除此檔案）。
ID 類設定填 0 代表「不啟用 / 使用預設」。
驗證、自動身分組、簽到、備份、警告、防炸群、歡迎離開等，直接用斜線指令設定，資料會存在 data/ 資料夾。
"""

# ───────────────────────── 必填 ─────────────────────────
TOKEN = "YOUR_DISCORD_BOT_TOKEN"

# ───────────────────────── 基本設定 ─────────────────────────
OWNER_IDS: list[int] = []   # 擁有者的使用者 ID（可使用 !sync / !reload）
PREFIX = "!"                # 前綴指令（僅擁有者管理用）

# ───────────────────────── Embed 外觀 ─────────────────────────
EMBED_COLOR = 0x5865F2
EMBED_FOOTER = "My Discord Bot"

# ───────────────────────── 狀態氣泡 ─────────────────────────
# 類型：custom / playing / watching / listening / competing
# • custom   ＝ 固定顯示的自訂狀態氣泡，不輪播（只會用第一筆）
# • 其他類型 ＝ 依序輪播
# 可用變數：{guilds} 伺服器數、{users} 成員總數、{prefix} 前綴
STATUS_INTERVAL = 30          # 輪播間隔（秒，最短 5）
STATUS_PRESENCE = "online"    # online / idle / dnd
STATUSES = [
    ("custom", "💬 @我就能聊天｜/help 查看指令"),
    ("playing", "輸入 /help 查看指令"),
    ("watching", "{guilds} 個伺服器"),
    ("listening", "{users} 位成員"),
]

# ───────────────────────── 事件紀錄 / 歡迎離開 / 自動身分組 ─────────────────────────
# 都用指令設定，每個伺服器各自獨立：
#   日誌頻道 /log_set　　歡迎與離開通知 /welcome_*　　加入自動給身分組 /autorole_*

# ───────────────────────── 管理與審核 ─────────────────────────
# 已改為用指令設定：/warn_setup（警告累積幾次自動禁言、禁言多久；不填參數 = 查看目前設定）

# ───────────────────────── 防炸群 ─────────────────────────
# 已改為用指令設定（每個伺服器各自獨立）：/security_status 查看，
# /security_toggle /security_spam /security_raid /security_nuke /security_whitelist 調整，/security_reset 恢復預設

# ───────────────────────── 伺服器備份 ─────────────────────────
# 已改為用指令設定（僅管理員）：/backup_auto（自動備份的間隔與保留份數；不填參數 = 查看目前設定）

# ───────────────────────── 黑名單 ─────────────────────────
# 被列入黑名單的人：不能使用機器人，並依下面設定處置
BLACKLIST_ACTION = "ban"                # ban（封鎖，也會預先封鎖不在伺服器的人）/ kick / none

# ───────────────────────── 數數字 ─────────────────────────
COUNTING_ALLOW_SAME_USER = False        # True = 允許同一個人連續數

# ───────────────────────── 每日簽到 ─────────────────────────
# 已改為用指令設定：/checkin_setup（點數、連續加成、換日時區）、/checkin_reward（連續獎勵身分組）、
# /checkin_reward_off、/checkin_status（查看目前設定）

# ───────────────────────── 語音掛機 / 音樂（Lavalink）─────────────────────────
# 自訂節點：填你自己的 Lavalink v4 伺服器。可放多個，連不上的會自動改用其他節點。
# uri 用 http:// 或 https://（https 會啟用 SSL）；整段留空 [] 會停用音樂與語音掛機功能。
LAVALINK_NODES = [
    {"identifier": "node01", "uri": "http://node01.example.com:2333", "password": "youshallnotpass"},
    # {"identifier": "node02", "uri": "https://node02.example.com:443", "password": "xxxxxx"},
]
VOICE_EMPTY_LEAVE_DELAY = 5             # /join 模式：真人都離開後，等幾秒再離開
MUSIC_SEARCH_SOURCE = "youtube"         # 預設的關鍵字搜尋來源：youtube / youtube_music / soundcloud / spotify
                                        # （/play 也能用「平台」欄位臨時指定；Spotify 需要節點安裝 LavaSrc 外掛，見 README 說明）
MUSIC_DEFAULT_VOLUME = 50               # 預設音量（1-100）
MUSIC_MAX_QUEUE = 50                    # 每個伺服器的佇列上限
MUSIC_IDLE_LEAVE_SECONDS = 180          # 沒在播放時，多久後自動離開（秒；/247 與 /join 模式不受影響）。0 = 不自動離開

# ───────────────────────── 按鈕與選單 ─────────────────────────
STAFF_ROLE_ID = 0           # 客服人員身分組（可查看 / 關閉客服單）
TICKET_CATEGORY_ID = 0      # 客服單建立在哪個分類底下（0 = 不放分類）

# 身分組選單（/rolepanel）。填入後重新啟動機器人即可
SELF_ROLES = [
    # {"label": "遊戲玩家", "emoji": "🎮", "role_id": 123456789012345678, "description": "想找人一起玩"},
    # {"label": "追劇中",   "emoji": "🎬", "role_id": 123456789012345679},
]

# ───────────────────────── AI 聊天（Groq）─────────────────────────
GROQ_API_KEY = ""                      # 到 https://console.groq.com/keys 申請（有免費額度）；留空 = 停用 AI 聊天
AI_MODEL = "llama-3.3-70b-versatile"   # 想要更快更省可改 "llama-3.1-8b-instant"；可用模型清單：https://console.groq.com/docs/models
AI_MAX_TOKENS = 1024                   # 單次回覆的長度上限
AI_TEMPERATURE = 0.7                   # 創意程度：0 = 穩定、1 = 活潑
AI_SYSTEM_PROMPT = (
    "你是 Discord 伺服器裡友善、幽默的 AI 助理。請使用繁體中文回答，語氣自然、簡潔。"
    "使用者訊息開頭的「名稱:」是發話者的暱稱。不要編造你不確定的事。"
)
AI_HISTORY_TURNS = 10                  # 每個頻道記住最近幾輪對話
AI_COOLDOWN = 5                        # 每位使用者的冷卻秒數
AI_CHANNEL_IDS: list[int] = []         # 這些頻道內的訊息不用 @ 機器人也會自動回覆
