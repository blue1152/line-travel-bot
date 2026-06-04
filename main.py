import os
import io
import re
import zipfile
import logging
import threading
import time
from html.parser import HTMLParser
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# --- 1. 初始化與環境變數 ---
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NETLIFY_AUTH_TOKEN = os.getenv("NETLIFY_AUTH_TOKEN") 
NETLIFY_SITE_ID = os.getenv("NETLIFY_SITE_ID")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
LINE_MAX_MESSAGE_LENGTH = 5000
LINE_MAX_MESSAGES = 5
COLD_START_WINDOW_SECONDS = int(os.getenv("COLD_START_WINDOW_SECONDS", "120"))
COLD_START_RETRY_DELAY_SECONDS = int(os.getenv("COLD_START_RETRY_DELAY_SECONDS", "60"))
LINE_BOT_USER_ID = os.getenv("LINE_BOT_USER_ID", "")
BOT_TRIGGER_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv("BOT_TRIGGER_KEYWORDS", "旅遊bot,旅行bot,導遊bot").split(",")
    if keyword.strip()
]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

user_chat_sessions = {}
user_session_locks = {}
session_registry_lock = threading.Lock()
app_started_at = time.time()
cold_start_lock = threading.Lock()
cold_start_first_valid_handled = False
processed_event_ids = {}
processed_event_lock = threading.Lock()

# --- 2. 核心：注入 Google Material 3 設計元件庫的 System Instruction ---
# --- 2. 核心：注入【行程/交通/景點/美食 + 官方地圖 URL】的 Material 3 視覺大腦 ---
TRAVEL_SYSTEM_INSTRUCTION = (
    "you are an expert travel planner and front-end UI/UX designer.\n\n"
    "【核心任務】：\n"
    "請依據使用者輸入的目的地與天數，規劃出兼具流暢度與深度體驗的旅遊行程。\n\n"
    "【LINE 對話回覆規範】：\n"
    "一般對話請盡量精簡，優先給重點、條列與可執行建議。不要使用 emoji 或顏文字。\n\n"
    "【最終網頁定稿 HTML 生成規範】：\n"
    "當使用者輸入『生成網頁』或『確認行程』時，代表這是最終定稿。你必須將這幾天討論好的完整行程，『完全轉化為標準的 HTML 原始碼』回傳，不要使用 emoji 或顏文字，並嚴格遵循以下 Google 官方 Material 3 視覺美學規範：\n\n"
    "1. 【引入 Material 3 設計元件庫與 Material Icons】：\n"
    "   請務必在 HTML <head> 區塊內引入以下 Web Components 腳本、字體與圖標庫：\n"
    "   <script type='module' src='https://esm.run/@material/web/all.js'></script>\n"
    "   <link href='https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Noto+Sans+TC:wght@400;500;700&display=swap' rel='stylesheet'>\n"
    "   <link href='https://fonts.googleapis.com/icon?family=Material+Icons' rel='stylesheet'>\n"
    "   <style>\n"
    "     body { font-family: 'Roboto', 'Noto Sans TC', sans-serif; background-color: #f4f5f7; margin: 0; padding: 16px; }\n"
    "     .day-card { background: white; border-radius: 16px; padding: 16px; margin-bottom: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }\n"
    "     .map-link { color: #6750A4; text-decoration: none; font-weight: bold; display: inline-flex; align-items: center; margin-top: 4px; }\n"
    "     .map-link span { font-size: 16px; margin-right: 4px; }\n"
    "   </style>\n\n"
    "2. 【四大元素與 Google 地圖 URL 的結構約束】：\n"
    "   在生成的行程網頁中，每一天的行程必須條理分明地包含以下內容，並利用 `<md-list>` 與 `<md-list-item>` 來排版：\n"
    "   * 【行程/時間軸】：利用 `<md-list-item>` 並設定標題為時間（如 09:00 - 11:00）。\n"
    "   * 【景點】：必須使用 Material Icon `<span class='material-icons' slot='start'>place</span>` 標註，且『每一個景點』下方都必須附上對應的 Google 地圖搜尋 URL，連結格式嚴格限制為：<a class='map-link' href='https://www.google.com/maps/search/?api=1&query=Time+Out+Market+Lisboa2'><span class='material-icons'>map</span>查看地圖</a>（請將店名與區域正確編碼）。\n"
    "   * 【交通】：必須使用 `<span class='material-icons' slot='start'>directions_car</span>` 或 `train` 等圖標，明確註明景點之間的移動方式（如：步行 10 分鐘或搭乘捷運板南線）。\n"
    "   * 【美食】：必須使用 `<span class='material-icons' slot='start'>restaurant</span>` 圖標標註周邊推薦的午晚餐或下午茶，且『每一間餐廳』下方也必須附上對應的 Google 地圖搜尋 URL 連結，格式同上。\n\n"
    "3. 【配色與純文字 HTML 規範】：\n"
    "   主色調使用 Material 3 沉穩的深藍/深紫（Primary: `#6750A4`），背景為淺灰。不要將 HTML 包裹在 Markdown 的 ```html 區塊內，直接輸出純文字的 HTML 程式碼即可。"
)

CSP_META_TAG = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; "
    "script-src https://esm.run; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src https:; "
    "connect-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "upgrade-insecure-requests"
    '">'
)
ALLOWED_MATERIAL_SCRIPT = "https://esm.run/@material/web/all.js"
ALLOWED_LINK_PREFIXES = (
    "https://fonts.googleapis.com/",
    "https://fonts.gstatic.com/",
)
DISALLOWED_TAGS = {
    "base",
    "embed",
    "form",
    "iframe",
    "object",
}
URL_ATTRS = {
    "action",
    "formaction",
    "href",
    "poster",
    "src",
    "xlink:href",
}

class StrictTravelHtmlValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.has_html = False
        self.has_head = False
        self.has_body = False
        self.has_material_script = False

    def handle_starttag(self, tag, attrs):
        self._validate_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._validate_tag(tag, attrs)

    def _validate_tag(self, tag, attrs):
        tag = tag.lower()
        attr_map = {name.lower(): (value or "") for name, value in attrs}

        if tag == "html":
            self.has_html = True
        elif tag == "head":
            self.has_head = True
        elif tag == "body":
            self.has_body = True

        if tag in DISALLOWED_TAGS:
            self.errors.append(f"不允許使用 <{tag}> 標籤")

        for name, value in attr_map.items():
            value = value.strip()
            value_lower = value.lower()
            if name.startswith("on"):
                self.errors.append(f"不允許使用事件屬性 {name}")
            if name in URL_ATTRS:
                self._validate_url_attr(tag, name, value, value_lower)

        if tag == "script":
            src = attr_map.get("src", "").strip()
            script_type = attr_map.get("type", "").strip().lower()
            if src == ALLOWED_MATERIAL_SCRIPT and script_type in {"module", ""}:
                self.has_material_script = True
            else:
                self.errors.append("只允許載入 Material Web 的外部 module script")

        if tag == "meta" and attr_map.get("http-equiv", "").lower() == "refresh":
            self.errors.append("不允許使用 meta refresh")

    def _validate_url_attr(self, tag, name, value, value_lower):
        if not value:
            return
        if value_lower.startswith(("#", "/", "mailto:", "tel:")):
            return
        if value_lower.startswith(("javascript:", "data:", "vbscript:", "file:")):
            self.errors.append(f"不安全的 URL 屬性 {name}")
            return
        if tag == "script" and name == "src":
            if value != ALLOWED_MATERIAL_SCRIPT:
                self.errors.append("script src 不在白名單")
            return
        if tag == "link" and name == "href":
            if not value.startswith(ALLOWED_LINK_PREFIXES):
                self.errors.append("link href 只允許 Google Fonts")
            return
        if not value_lower.startswith("https://"):
            self.errors.append(f"URL 屬性 {name} 必須使用 https")

def extract_html(raw_content: str) -> str:
    content = (raw_content or "").strip()
    fence_match = re.search(r"```(?:html)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        content = fence_match.group(1).strip()

    doc_match = re.search(r"(?:<!doctype\s+html[^>]*>\s*)?<html\b.*?</html>", content, flags=re.IGNORECASE | re.DOTALL)
    if not doc_match:
        raise ValueError("Gemini 回傳內容沒有完整的 <html> 文件")

    html_content = doc_match.group(0).strip()
    if not re.match(r"<!doctype\s+html", html_content, flags=re.IGNORECASE):
        html_content = "<!doctype html>\n" + html_content
    return html_content

def inject_csp(html_content: str) -> str:
    html_content = re.sub(
        r"<meta\b[^>]*http-equiv\s*=\s*['\"]?content-security-policy['\"]?[^>]*>",
        "",
        html_content,
        flags=re.IGNORECASE,
    )
    if not re.search(r"<head\b[^>]*>", html_content, flags=re.IGNORECASE):
        raise ValueError("HTML 缺少 <head> 區塊")
    return re.sub(
        r"(<head\b[^>]*>)",
        r"\1\n    " + CSP_META_TAG,
        html_content,
        count=1,
        flags=re.IGNORECASE,
    )

def validate_html_is_safe(html_content: str):
    validator = StrictTravelHtmlValidator()
    validator.feed(html_content)
    validator.close()

    if not validator.has_html:
        validator.errors.append("HTML 缺少 <html> 標籤")
    if not validator.has_head:
        validator.errors.append("HTML 缺少 <head> 標籤")
    if not validator.has_body:
        validator.errors.append("HTML 缺少 <body> 標籤")
    if not validator.has_material_script:
        validator.errors.append("HTML 缺少 Material Web script")

    if validator.errors:
        raise ValueError("；".join(validator.errors))

def sanitize_generated_html(raw_content: str) -> str:
    html_content = extract_html(raw_content)
    html_content = inject_csp(html_content)
    validate_html_is_safe(html_content)
    return html_content

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_request, body_str, signature)
    return "OK"

def handle_line_request(body: str, signature: str):
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("LINE 簽章驗證失敗。")

def get_user_session_lock(user_id: str) -> threading.Lock:
    with session_registry_lock:
        if user_id not in user_session_locks:
            user_session_locks[user_id] = threading.Lock()
        return user_session_locks[user_id]

def cleanup_expired_sessions():
    now = time.time()
    with session_registry_lock:
        expired_user_ids = [
            user_id
            for user_id, session in user_chat_sessions.items()
            if now - session["updated_at"] > SESSION_TTL_SECONDS
        ]

    for user_id in expired_user_ids:
        user_lock = get_user_session_lock(user_id)
        if not user_lock.acquire(blocking=False):
            continue
        try:
            with session_registry_lock:
                session = user_chat_sessions.get(user_id)
                if session and now - session["updated_at"] > SESSION_TTL_SECONDS:
                    user_chat_sessions.pop(user_id, None)
        finally:
            user_lock.release()

def reset_user_chat_session(user_id: str):
    user_lock = get_user_session_lock(user_id)
    with user_lock:
        with session_registry_lock:
            user_chat_sessions.pop(user_id, None)

def split_line_messages(text: str) -> list[TextMessage]:
    normalized_text = text or " "
    max_total_length = LINE_MAX_MESSAGE_LENGTH * LINE_MAX_MESSAGES
    truncated_text = normalized_text[:max_total_length]

    return [
        TextMessage(text=truncated_text[index:index + LINE_MAX_MESSAGE_LENGTH])
        for index in range(0, len(truncated_text), LINE_MAX_MESSAGE_LENGTH)
    ] or [TextMessage(text=" ")]

RESET_COMMANDS = {"重置", "新行程", "reset"}
GENERATE_COMMANDS = {"生成網頁", "確認行程", "打包網頁"}
DEDUP_TTL_SECONDS = 600

def get_line_target_id(event: MessageEvent) -> str:
    source = event.source
    return (
        getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
        or getattr(source, "user_id", None)
        or ""
    )

def is_group_or_room_event(event: MessageEvent) -> bool:
    source = event.source
    return bool(getattr(source, "group_id", None) or getattr(source, "room_id", None))

def event_mentions_bot(event: MessageEvent) -> bool:
    mention = getattr(event.message, "mention", None)
    mentionees = getattr(mention, "mentionees", []) if mention else []
    if not mentionees:
        return False
    if not LINE_BOT_USER_ID:
        return False
    return any(getattr(mentionee, "user_id", "") == LINE_BOT_USER_ID for mentionee in mentionees)

def strip_bot_trigger(text: str) -> str:
    stripped_text = text.strip()
    for keyword in BOT_TRIGGER_KEYWORDS:
        stripped_text = re.sub(rf"@?{re.escape(keyword)}", "", stripped_text, flags=re.IGNORECASE).strip()
    return stripped_text or text.strip()

def is_bot_triggered(event: MessageEvent, text: str) -> bool:
    stripped_text = text.strip()
    if stripped_text in RESET_COMMANDS or stripped_text in GENERATE_COMMANDS:
        return True
    if event_mentions_bot(event):
        return True
    return any(keyword.lower() in stripped_text.lower() for keyword in BOT_TRIGGER_KEYWORDS)

def cleanup_processed_event_ids():
    now = time.time()
    with processed_event_lock:
        expired_event_ids = [
            event_id
            for event_id, processed_at in processed_event_ids.items()
            if now - processed_at > DEDUP_TTL_SECONDS
        ]
        for event_id in expired_event_ids:
            processed_event_ids.pop(event_id, None)

def is_duplicate_event(event: MessageEvent, skip_dedupe: bool = False) -> bool:
    if skip_dedupe:
        return False

    cleanup_processed_event_ids()
    event_id = getattr(event, "webhook_event_id", None)
    if not event_id:
        event_id = f"{get_line_target_id(event)}:{getattr(event, 'timestamp', '')}:{event.message.text}"

    with processed_event_lock:
        if event_id in processed_event_ids:
            return True
        processed_event_ids[event_id] = time.time()
        return False

def should_delay_for_cold_start() -> bool:
    global cold_start_first_valid_handled

    with cold_start_lock:
        if cold_start_first_valid_handled:
            return False
        if time.time() - app_started_at > COLD_START_WINDOW_SECONDS:
            cold_start_first_valid_handled = True
            return False
        cold_start_first_valid_handled = True
        return True

# --- 5. 處理「文字」訊息事件 (自動追加定稿引導提示) ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    if is_duplicate_event(event):
        logger.info("略過重複的 LINE webhook event")
        return

    raw_user_message = event.message.text
    reply_token = event.reply_token
    user_id = event.source.user_id
    target_id = get_line_target_id(event)
    
    if not user_id or not target_id:
        return

    if is_group_or_room_event(event) and not is_bot_triggered(event, raw_user_message):
        return

    user_message = strip_bot_trigger(raw_user_message)
    if not user_message:
        return

    if should_delay_for_cold_start():
        send_line_reply(reply_token, "休息中, 請稍等")
        time.sleep(COLD_START_RETRY_DELAY_SECONDS)
        process_user_text(user_id, target_id, user_message, lambda text: send_line_push(target_id, text))
        return

    process_user_text(user_id, target_id, user_message, lambda text: send_line_reply(reply_token, text))

def process_user_text(user_id: str, target_id: str, user_message: str, send_response):
    # 重置記憶指令
    if user_message.strip() in RESET_COMMANDS:
        reset_user_chat_session(user_id)
        send_response("已清空記憶。請問下一個想去哪裡度假？")
        return

    # --- 階段 A：使用者決定定稿，生成網頁 ---
    if user_message.strip() in GENERATE_COMMANDS:
        logger.info(f"使用者 {user_id} 觸發 Material 3 網頁定稿生成...")
        send_response("收到，我正在整理行程並生成網頁。完成後會直接把 Netlify 連結傳給你。")
        generate_and_push_itinerary_page(user_id, target_id)
        return

    # --- 階段 B：常規對話（每一次回答後面都加上提示詞） ---
    # 讓 Gemini 正常回答使用者的景點調整需求
    ai_response = ask_gemini_travel_agent(user_id, user_message)

    # 【核心優化】：用 Python 在後端動態黏上引導提示字串
    cta_hint = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*【導遊提示】*\n"
        "如果目前的行程內容你很滿意，隨時對我打 **「確認行程」** 或 **「生成網頁」**，我會立刻打包成 Material 3 排版的專屬行程網頁。"
    )

    # 組裝最終回傳給 Line 的訊息
    final_line_text = ai_response + cta_hint
    send_response(final_line_text)

def ask_gemini_travel_agent(user_id: str, prompt: str) -> str:
    try:
        cleanup_expired_sessions()
        user_lock = get_user_session_lock(user_id)

        with user_lock:
            with session_registry_lock:
                session = user_chat_sessions.get(user_id)
            if not session:
                session = {
                    "chat": gemini_client.chats.create(
                        model='gemini-3.1-flash-lite',
                        config=types.GenerateContentConfig(
                            system_instruction=TRAVEL_SYSTEM_INSTRUCTION,
                            temperature=0.3, # 保持適度穩定度來輸出複雜的 HTML 結構
                            tools=[types.Tool(google_search=types.GoogleSearch())]
                        )
                    ),
                    "updated_at": time.time()
                }
                with session_registry_lock:
                    user_chat_sessions[user_id] = session

            response = session["chat"].send_message(prompt)
            session["updated_at"] = time.time()
            return response.text if response.text else ""
    except Exception as e:
        logger.exception(f"Gemini Error: {e}")
        return "系統打結，請輸入『新行程』重新試試看！"

def generate_and_push_itinerary_page(user_id: str, target_id: str):
    raw_html = ask_gemini_travel_agent(user_id, "請將我們目前討論好的最終行程，立刻依照 Material 3 格式規範輸出為完整的 HTML 網頁原始碼。")
    try:
        html_code = sanitize_generated_html(raw_html)
    except ValueError as e:
        logger.warning(f"Gemini HTML 驗證失敗，已停止部署: {e}")
        send_line_push(target_id, "網頁內容沒有通過安全檢查，所以我沒有部署。請再輸入「生成網頁」讓我重新產生一次。")
        return

    netlify_url = deploy_html_to_netlify(html_code)

    if netlify_url:
        push_text = (
            "你的 Material 3 旅遊行程網頁已製作完成。\n\n"
            "本導遊已經幫你套用 Google 官方設計元件，並自動託管上線囉！點擊下方連結即可查看：\n"
            f"{netlify_url}"
        )
    else:
        push_text = "網頁生成或 Netlify 部署失敗，請稍後再輸入「生成網頁」試一次。"

    send_line_push(target_id, push_text)

def deploy_html_to_netlify(html_content: str) -> str:
    try:
        if not NETLIFY_AUTH_TOKEN:
            logger.error("缺少 NETLIFY_AUTH_TOKEN 環境變數")
            return ""

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr("index.html", html_content)
        
        zip_buffer.seek(0)
        zip_binary_data = zip_buffer.getvalue()

        headers = {
            "Authorization": f"Bearer {NETLIFY_AUTH_TOKEN}",
            "Content-Type": "application/zip"
        }

        logger.info("正在發送 M3 HTML 至 Netlify API...")
        if NETLIFY_SITE_ID:
            url = f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys"
        else:
            url = "https://api.netlify.com/api/v1/sites"

        response = requests.post(url, headers=headers, data=zip_binary_data, timeout=15)
        
        if response.status_code in [200, 201]:
            return response.json().get("ssl_url")
        else:
            logger.error(f"Netlify Error: {response.status_code} - {response.text}")
            return ""
    except Exception as e:
        logger.exception(f"deploy_html_to_netlify Exception: {e}")
        return ""

def send_line_reply(reply_token: str, text: str):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=split_line_messages(text)
                )
            )
    except Exception as e:
        logger.error(f"發送 LINE 回覆訊息失敗: {e}")

def send_line_push(target_id: str, text: str):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=split_line_messages(text)
                )
            )
    except Exception as e:
        logger.error(f"發送 LINE Push 訊息失敗: {e}")
