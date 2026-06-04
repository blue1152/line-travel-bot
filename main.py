import os
import io
import re
import uuid
import zipfile
import logging
import threading
import time
from html import escape, unescape
from html.parser import HTMLParser
from datetime import datetime
from urllib.parse import quote, urlparse
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
NETLIFY_ACCOUNT_SLUG = os.getenv("NETLIFY_ACCOUNT_SLUG", "")
NETLIFY_DEPLOY_POLL_SECONDS = int(os.getenv("NETLIFY_DEPLOY_POLL_SECONDS", "30"))
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
    "當使用者輸入『生成網頁』或『確認行程』時，代表這是最終定稿。HTML 原始碼只供後端部署使用，不要在一般 LINE 對話中解釋或展示 HTML。你必須將這幾天討論好的完整行程，『完全轉化為標準的 HTML 原始碼』回傳，不要使用 emoji 或顏文字，並嚴格遵循以下 Google 官方 Material 3 視覺美學規範：\n\n"
    "1. 【引入 Material 3 設計元件庫與 Material Icons】：\n"
    "   請務必在 HTML <head> 區塊內引入以下字體與圖標庫。不要輸出任何 `<script>` 標籤：\n"
    "   <link href='https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Noto+Sans+TC:wght@400;500;700&display=swap' rel='stylesheet'>\n"
    "   <link href='https://fonts.googleapis.com/icon?family=Material+Icons' rel='stylesheet'>\n"
    "   <style>\n"
    "     :root { --md-sys-color-primary: #6750A4; --md-sys-color-on-primary: #FFFFFF; --md-sys-color-primary-container: #E8DEF8; --md-sys-color-on-primary-container: #21005D; }\n"
    "     body { font-family: 'Roboto', 'Noto Sans TC', sans-serif; background-color: #f4f5f7; margin: 0; padding: 16px; }\n"
    "     .container { max-width: 600px; width: 100%; margin: 0 auto; }\n"
    "     .banner { background-color: var(--md-sys-color-primary); color: var(--md-sys-color-on-primary); padding: 32px 22px; border-radius: 16px; margin-bottom: 24px; text-align: center; box-shadow: 0 6px 16px rgba(103, 80, 164, 0.22); }\n"
    "     .banner h1 { margin: 0; font-size: 28px; font-weight: 700; }\n"
    "     .banner p { margin: 10px 0 0; opacity: .9; font-size: 15px; }\n"
    "     .day-card { background: white; border-radius: 20px; padding: 22px; margin-bottom: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.04); border: 1px solid rgba(0,0,0,0.02); }\n"
    "     .day-badge { display: inline-flex; align-items: center; gap: 6px; background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container); padding: 6px 16px; border-radius: 24px; font-weight: 700; font-size: 14px; margin-bottom: 20px; }\n"
    "     .timeline-item { border-left: 3px solid var(--md-sys-color-primary-container); padding-left: 20px; margin-bottom: 24px; position: relative; }\n"
    "     .time-tag { font-weight: 700; color: var(--md-sys-color-primary); font-size: 14px; margin-bottom: 6px; }\n"
    "     .item-title { font-size: 17px; font-weight: 700; display: flex; align-items: center; color: #1C1B1F; gap: 6px; }\n"
    "     .item-desc { font-size: 14px; color: #49454F; margin-top: 6px; line-height: 1.5; }\n"
    "     .map-link { color: var(--md-sys-color-primary); text-decoration: none; font-weight: 700; font-size: 13px; display: inline-flex; align-items: center; margin-top: 8px; padding: 4px 0; gap: 4px; }\n"
    "     .map-link span { font-size: 16px; margin-right: 4px; }\n"
    "   </style>\n\n"
    "2. 【標題與內容結構】：\n"
    "   網頁最上方必須有 `<div class='banner'>`。banner 的 `<h1>` 格式為「Gemini 自己發想的 4-8 字詩意主題 + ・ + 行程氣質短句」，例如「洄瀾山海・慢活時光」。banner 的 `<p>` 必須包含使用者提到的地點與時間，例如「花蓮 3 天 2 夜深度自駕提案」。\n"
    "   每一天使用 `<div class='day-card'>`，開頭用 `<div class='day-badge'><span class='material-icons'>calendar_today</span>第一天：Gemini 發想的當日主題</div>`。day badge 文字必須固定為「第幾天：當日主題」，不要使用 DAY 1。\n"
    "   每一天內容固定輸出三類區塊，順序為：時間軸、交通提示、美食推薦。每個區塊都用 `<div class='timeline-item'>`，內含 `<div class='time-tag'>`、`<div class='item-title'><span class='material-icons'>...</span>標題</div>`、`<div class='item-desc'>文字敘述</div>`。\n"
    "   * 【時間軸】：`time-tag` 放明確時間（如 10:00 - 12:00），`item-title` 放景點/活動標題，`item-desc` 放體驗描述。\n"
    "   * 【交通提示】：`time-tag` 寫「交通提示」，`item-title` 放交通方式標題（如 自駕 / 租車前往），`item-desc` 放路線與時間。\n"
    "   * 【美食推薦】：`time-tag` 寫「美食推薦」，`item-title` 放餐廳/小吃標題，`item-desc` 放推薦原因。\n"
    "   景點與美食推薦下方必須附上 Google 地圖搜尋 URL，連結格式嚴格限制為：<a class='map-link' target='_blank' rel='noopener noreferrer' href='https://www.google.com/maps/search/?api=1&query=Time+Out+Market+Lisboa2'><span class='material-icons'>map</span>查看地圖導航</a>（請將店名與區域正確編碼）。\n"
    "   不要使用 `<md-list>` 或 `<md-list-item>`。\n\n"
    "3. 【配色與純文字 HTML 規範】：\n"
    "   主色調使用 Material 3 沉穩的深藍/深紫（Primary: `#6750A4`），背景為淺灰。不要將 HTML 包裹在 Markdown 的 ```html 區塊內，不要放在 <pre> 標籤內，不要輸出 escaped HTML（例如 &lt;html&gt;），直接輸出可由瀏覽器渲染的完整 HTML 原始碼即可。"
)

CSP_META_TAG = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; "
    "script-src 'none'; "
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
            self.errors.append("不允許使用 script 標籤")

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

def strip_markdown_fence(content: str) -> str:
    fence_match = re.search(r"```(?:html)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return content.strip()

def strip_wrapping_quotes(content: str) -> str:
    stripped_content = content.strip()
    if len(stripped_content) >= 2 and stripped_content[0] == stripped_content[-1] and stripped_content[0] in {"'", '"'}:
        return stripped_content[1:-1].strip()
    return stripped_content

def unwrap_pre_html(content: str) -> str:
    pre_match = re.search(r"<pre\b[^>]*>(.*?)</pre>", content, flags=re.IGNORECASE | re.DOTALL)
    if not pre_match:
        return content

    pre_content = pre_match.group(1).strip()
    unescaped_pre_content = unescape(pre_content).strip()
    if re.search(r"(?:<!doctype\s+html[^>]*>\s*)?<html\b", unescaped_pre_content, flags=re.IGNORECASE):
        return unescaped_pre_content
    return content

def normalize_generated_html_text(raw_content: str) -> str:
    content = (raw_content or "").strip()
    for _ in range(3):
        previous_content = content
        content = strip_markdown_fence(content)
        content = strip_wrapping_quotes(content)
        content = unwrap_pre_html(content)
        if not re.search(r"<html\b", content, flags=re.IGNORECASE):
            content = unescape(content).strip()
        if content == previous_content:
            break
    return content

def extract_html(raw_content: str) -> str:
    content = normalize_generated_html_text(raw_content)
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

def extract_attr(attrs: str, name: str) -> str:
    attr_match = re.search(rf"\b{name}\s*=\s*(['\"])(.*?)\1", attrs, flags=re.IGNORECASE | re.DOTALL)
    return unescape(attr_match.group(2).strip()) if attr_match else ""

def remove_slot_attr(tag_html: str) -> str:
    return re.sub(r"\s+slot\s*=\s*(['\"]).*?\1", "", tag_html, flags=re.IGNORECASE | re.DOTALL)

def convert_md_list_item(match) -> str:
    attrs = match.group(1)
    inner_html = match.group(2)

    headline = extract_attr(attrs, "headline")
    headline_match = re.search(
        r"<div\b[^>]*slot\s*=\s*(['\"])headline\1[^>]*>(.*?)</div>",
        inner_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if headline_match:
        headline = unescape(re.sub(r"<[^>]+>", "", headline_match.group(2)).strip()) or headline

    icon_html = ""
    icon_match = re.search(
        r"<span\b[^>]*slot\s*=\s*(['\"])start\1[^>]*>.*?</span>",
        inner_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if icon_match:
        icon_html = remove_slot_attr(icon_match.group(0))

    supporting_blocks = [
        block.strip()
        for _, block in re.findall(
            r"<div\b[^>]*slot\s*=\s*(['\"])supporting-text\1[^>]*>(.*?)</div>",
            inner_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]
    supporting_html = "".join(f"<div class='item-supporting'>{block}</div>" for block in supporting_blocks)

    remainder = inner_html
    remainder = re.sub(r"<span\b[^>]*slot\s*=\s*(['\"])start\1[^>]*>.*?</span>", "", remainder, flags=re.IGNORECASE | re.DOTALL)
    remainder = re.sub(r"<div\b[^>]*slot\s*=\s*(['\"])(?:headline|supporting-text)\1[^>]*>.*?</div>", "", remainder, flags=re.IGNORECASE | re.DOTALL)
    remainder = remainder.strip()

    title_html = f"<div class='item-title'>{escape(headline)}</div>" if headline else ""
    return (
        "<div class='itinerary-item'>"
        f"<div class='item-icon'>{icon_html}</div>"
        "<div class='item-main'>"
        f"{title_html}{supporting_html}{remainder}"
        "</div>"
        "</div>"
    )

def normalize_material_lists(html_content: str) -> str:
    html_content = re.sub(
        r"<md-list-item\b([^>]*)>(.*?)</md-list-item>",
        convert_md_list_item,
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html_content = re.sub(r"<md-list(?=[\s>])[^>]*>", "<div class='itinerary-list'>", html_content, flags=re.IGNORECASE)
    return re.sub(r"</md-list>", "</div>", html_content, flags=re.IGNORECASE)

def normalize_itinerary_structure(html_content: str) -> str:
    html_content = re.sub(
        r"<h2>(.*?)</h2>",
        r"<div class='day-badge'><span class='material-icons'>calendar_today</span>\1</div>",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html_content = re.sub(r"\bitinerary-item\b", "timeline-item", html_content, flags=re.IGNORECASE)
    html_content = re.sub(r"\btime\b", "time-tag", html_content, flags=re.IGNORECASE)
    html_content = re.sub(r"\btitle\b", "item-title", html_content, flags=re.IGNORECASE)
    html_content = re.sub(r"\bdesc\b", "item-desc", html_content, flags=re.IGNORECASE)
    html_content = re.sub(r"\bmap-btn\b", "map-link", html_content, flags=re.IGNORECASE)

    if "class=\"container\"" not in html_content and "class='container'" not in html_content:
        html_content = re.sub(r"<body\b([^>]*)>", r"<body\1><div class='container'>", html_content, count=1, flags=re.IGNORECASE)
        html_content = re.sub(r"</body>", r"</div></body>", html_content, count=1, flags=re.IGNORECASE)
    return html_content

def remove_disallowed_scripts(html_content: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html_content, flags=re.IGNORECASE | re.DOTALL)

def normalize_day_badge_text(html_content: str) -> str:
    day_names = {
        "1": "第一天",
        "2": "第二天",
        "3": "第三天",
        "4": "第四天",
        "5": "第五天",
        "6": "第六天",
        "7": "第七天",
        "8": "第八天",
        "9": "第九天",
        "10": "第十天",
    }

    def replace_day(match):
        day_name = day_names.get(match.group(1), f"第{match.group(1)}天")
        return f"{day_name}："

    return re.sub(r"\bDAY\s*(\d+)\s*[：:]", replace_day, html_content, flags=re.IGNORECASE)

def remove_invalid_item_title_tags(html_content: str) -> str:
    return re.sub(
        r"\s*<item-title\b[^>]*>.*?</item-title>\s*",
        "",
        html_content,
        flags=re.IGNORECASE | re.DOTALL,
    )

def inject_itinerary_fallback_css(html_content: str) -> str:
    css = (
        "<style>"
        ":root{--md-sys-color-primary:#6750A4;--md-sys-color-on-primary:#FFFFFF;--md-sys-color-primary-container:#E8DEF8;--md-sys-color-on-primary-container:#21005D;}"
        "body{font-family:'Roboto','Noto Sans TC',sans-serif;background-color:#f4f5f7;margin:0;padding:16px;}"
        ".container{max-width:600px;width:100%;margin:0 auto;}"
        ".banner{background-color:var(--md-sys-color-primary);color:var(--md-sys-color-on-primary);padding:32px 22px;border-radius:16px;margin-bottom:24px;text-align:center;box-shadow:0 6px 16px rgba(103,80,164,.22);}"
        ".banner h1{margin:0;font-size:28px;font-weight:700;letter-spacing:.5px;line-height:1.35;}.banner p{margin:10px 0 0;opacity:.9;font-size:15px;}"
        ".day-card{background:#fff;border-radius:20px;padding:22px;margin-bottom:24px;box-shadow:0 4px 16px rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.02);}"
        ".day-badge{display:inline-flex;align-items:center;background:var(--md-sys-color-primary-container);color:var(--md-sys-color-on-primary-container);padding:6px 16px;border-radius:24px;font-weight:700;font-size:14px;margin-bottom:20px;gap:6px;}"
        ".day-badge .material-icons{font-size:18px;}"
        ".itinerary-list{display:block;margin-top:0;}"
        ".timeline-item{border-left:3px solid var(--md-sys-color-primary-container);padding-left:20px;margin-bottom:24px;position:relative;writing-mode:horizontal-tb;word-break:normal;overflow-wrap:anywhere;}"
        ".timeline-item:last-child{margin-bottom:0;}"
        ".time-tag{font-weight:700;color:var(--md-sys-color-primary);font-size:14px;margin:0 0 8px;line-height:1.45;}"
        ".item-title{font-size:17px;font-weight:700;display:flex;align-items:center;color:#1C1B1F;gap:6px;line-height:1.5;margin:0;}"
        ".item-title .material-icons,.item-icon .material-icons{font-size:20px;color:var(--md-sys-color-primary);}"
        ".item-desc,.item-supporting{font-size:14px;color:#49454F;margin-top:6px;line-height:1.55;}"
        ".item-main{min-width:0;line-height:1.6;}"
        ".map-link{color:var(--md-sys-color-primary);text-decoration:none;font-weight:700;font-size:13px;display:inline-flex;align-items:center;margin-top:8px;padding:4px 0;gap:4px;white-space:normal;}"
        ".map-link:hover{opacity:.7;}.map-link .material-icons{font-size:16px;}"
        ".generated-date-footer{max-width:600px;margin:32px auto 8px;padding:16px;color:#666;text-align:center;font-size:.9rem;line-height:1.6;}"
        "@media(max-width:640px){body{padding:12px;}.banner{padding:26px 16px;}.banner h1{font-size:24px;}.day-card{border-radius:18px;padding:20px;}.timeline-item{padding-left:16px;}}"
        "</style>"
    )
    return re.sub(r"(</head>)", css + r"\1", html_content, count=1, flags=re.IGNORECASE)

def ensure_map_links_open_new_tab(html_content: str) -> str:
    def update_anchor(match):
        attrs = match.group(1)
        if not re.search(r"\bclass\s*=\s*(['\"])[^'\"]*\bmap-link\b[^'\"]*\1", attrs, flags=re.IGNORECASE):
            return match.group(0)
        if not re.search(r"\btarget\s*=", attrs, flags=re.IGNORECASE):
            attrs += ' target="_blank"'
        if re.search(r"\brel\s*=", attrs, flags=re.IGNORECASE):
            attrs = re.sub(
                r"\brel\s*=\s*(['\"])[^'\"]*\1",
                'rel="noopener noreferrer"',
                attrs,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            attrs += ' rel="noopener noreferrer"'
        return f"<a{attrs}>"

    return re.sub(r"<a\b([^>]*)>", update_anchor, html_content, flags=re.IGNORECASE | re.DOTALL)

def inject_generated_date_footer(html_content: str) -> str:
    generated_date = datetime.now().strftime("%Y-%m-%d")
    footer_html = (
        "\n<footer class='generated-date-footer'>"
        f"<div>生成日期：{generated_date}</div>"
        "<div>Website Designed &amp; Developed by Gemini and Netlify</div>"
        "</footer>\n"
    )
    if not re.search(r"</body>", html_content, flags=re.IGNORECASE):
        raise ValueError("HTML 缺少 </body> 區塊")
    return re.sub(r"</body>", footer_html + "</body>", html_content, count=1, flags=re.IGNORECASE)

def validate_html_is_safe(html_content: str):
    validator = StrictTravelHtmlValidator()
    validator.feed(html_content)
    validator.close()

    if len(re.findall(r"<html\b", html_content, flags=re.IGNORECASE)) != 1:
        validator.errors.append("HTML 必須只有一個 <html> 標籤")
    if re.search(r"<pre\b", html_content, flags=re.IGNORECASE):
        validator.errors.append("HTML 不允許殘留 <pre> 包裝")
    if re.search(r"</?md-list(?=[\s>])", html_content, flags=re.IGNORECASE):
        validator.errors.append("HTML 不允許殘留 Material list 元件")
    if not validator.has_html:
        validator.errors.append("HTML 缺少 <html> 標籤")
    if not validator.has_head:
        validator.errors.append("HTML 缺少 <head> 標籤")
    if not validator.has_body:
        validator.errors.append("HTML 缺少 <body> 標籤")
    if validator.errors:
        raise ValueError("；".join(validator.errors))

def sanitize_generated_html(raw_content: str) -> str:
    html_content = extract_html(raw_content)
    html_content = remove_disallowed_scripts(html_content)
    html_content = normalize_material_lists(html_content)
    html_content = normalize_itinerary_structure(html_content)
    html_content = remove_invalid_item_title_tags(html_content)
    html_content = normalize_day_badge_text(html_content)
    html_content = ensure_map_links_open_new_tab(html_content)
    html_content = inject_generated_date_footer(html_content)
    html_content = inject_itinerary_fallback_css(html_content)
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
GENERATE_NEW_PAGE_COMMANDS = {"生成新網頁"}
UPDATE_EXISTING_PAGE_COMMAND = "修改舊網頁"
DEDUP_TTL_SECONDS = 600

def is_update_existing_page_command(text: str) -> bool:
    return text.strip().startswith(UPDATE_EXISTING_PAGE_COMMAND)

def extract_first_url(text: str) -> str:
    url_match = re.search(r"https?://[^\s<>]+", text or "")
    return url_match.group(0).rstrip(".,;，。；") if url_match else ""

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
    if (
        stripped_text in RESET_COMMANDS
        or stripped_text in GENERATE_COMMANDS
        or stripped_text in GENERATE_NEW_PAGE_COMMANDS
        or is_update_existing_page_command(stripped_text)
    ):
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
    if user_message.strip() in GENERATE_NEW_PAGE_COMMANDS:
        logger.info(f"使用者 {user_id} 觸發 Material 3 新網頁定稿生成...")
        send_response("收到，我正在整理行程並生成新的 Netlify 網頁。完成後會直接把連結傳給你。")
        generate_and_push_itinerary_page(user_id, target_id, force_new_site=True)
        return

    if is_update_existing_page_command(user_message):
        existing_page_url = extract_first_url(user_message)
        if not existing_page_url:
            send_response("請在「修改舊網頁」後面貼上既有 Netlify 網址，例如：\n修改舊網頁 https://your-site.netlify.app/")
            return

        logger.info(f"使用者 {user_id} 觸發既有 Netlify 網頁更新: {existing_page_url}")
        send_response("收到，我會用這次行程內容覆蓋你貼的既有 Netlify 頁面。完成後會把更新後連結傳給你。")
        generate_and_push_itinerary_page(user_id, target_id, existing_page_url=existing_page_url)
        return

    if user_message.strip() in GENERATE_COMMANDS:
        logger.info(f"使用者 {user_id} 觸發 Material 3 網頁定稿生成...")
        send_response("收到，我正在整理行程並生成網頁。完成後會直接把 Netlify 連結傳給你。")
        generate_and_push_itinerary_page(user_id, target_id)
        return

    # --- 階段 B：常規對話（每一次回答後面都加上提示詞） ---
    # 讓 Gemini 正常回答使用者的景點調整需求
    ai_response = ask_gemini_travel_agent(user_id, user_message)
    if looks_like_html_document(ai_response):
        logger.warning("Gemini 在一般對話中回傳 HTML，已阻擋 LINE 輸出。")
        send_response("我已準備好行程資料。若要產生網頁，請輸入「生成網頁」。")
        return

    # 【核心優化】：用 Python 在後端動態黏上引導提示字串
    cta_hint = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*【導遊提示】*\n"
        "如果目前的行程內容你很滿意，可以輸入 **「生成新網頁」** 建立新的 Netlify 頁面；或輸入 **「修改舊網頁 既有網址」** 覆蓋已存在的頁面。"
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

def looks_like_html_document(text: str) -> bool:
    return bool(re.search(r"<!doctype\s+html|<html\b|</html>|<body\b|</body>", text or "", flags=re.IGNORECASE))

def generate_itinerary_html(user_id: str) -> str:
    try:
        prompt = (
            "請根據目前對話中已討論好的最終行程，立刻依照 Material 3 格式規範輸出完整 HTML 網頁原始碼。"
            "這段 HTML 只供後端部署使用，不要加入任何說明文字、摘要、Markdown 或給使用者看的聊天內容。"
        )
        user_lock = get_user_session_lock(user_id)
        with user_lock:
            with session_registry_lock:
                session = user_chat_sessions.get(user_id)
            if session:
                temp_chat = gemini_client.chats.create(
                    model='gemini-3.1-flash-lite',
                    config=types.GenerateContentConfig(
                        system_instruction=TRAVEL_SYSTEM_INSTRUCTION,
                        temperature=0.2,
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    ),
                    history=session["chat"].get_history(curated=True)
                )
                response = temp_chat.send_message(prompt)
                session["updated_at"] = time.time()
                return response.text if response.text else ""

            response = gemini_client.models.generate_content(
                model='gemini-3.1-flash-lite',
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=TRAVEL_SYSTEM_INSTRUCTION,
                    temperature=0.2,
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            return response.text if response.text else ""
    except Exception as e:
        logger.exception(f"Gemini HTML generation error: {e}")
        return ""

def generate_and_push_itinerary_page(
    user_id: str,
    target_id: str,
    existing_page_url: str = "",
    force_new_site: bool = False,
):
    raw_html = generate_itinerary_html(user_id)
    if not raw_html:
        send_line_push(target_id, "目前沒有足夠的行程內容可以生成網頁，請先告訴我目的地、天數與偏好。")
        return

    try:
        html_code = sanitize_generated_html(raw_html)
    except ValueError as e:
        logger.warning(f"Gemini HTML 驗證失敗，已停止部署: {e}")
        send_line_push(target_id, "網頁內容沒有通過安全檢查，所以我沒有部署。請再輸入「生成網頁」讓我重新產生一次。")
        return

    netlify_url = deploy_html_to_netlify(
        html_code,
        existing_page_url=existing_page_url,
        force_new_site=force_new_site,
    )

    if netlify_url:
        action_text = "已更新" if existing_page_url else "已製作完成"
        push_text = (
            f"你的 Material 3 旅遊行程網頁{action_text}。\n\n"
            "本導遊已經幫你套用 Google 官方設計元件，並完成 Netlify 部署。點擊下方連結即可查看：\n"
            f"{netlify_url}"
        )
    else:
        push_text = "網頁生成或 Netlify 部署失敗，請稍後再輸入「生成新網頁」或「修改舊網頁 既有網址」試一次。"

    send_line_push(target_id, push_text)

def deploy_html_to_netlify(
    html_content: str,
    existing_page_url: str = "",
    force_new_site: bool = False,
) -> str:
    try:
        if not NETLIFY_AUTH_TOKEN:
            logger.error("缺少 NETLIFY_AUTH_TOKEN 環境變數")
            return ""

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr("index.html", html_content)
            zip_file.writestr(
                "_headers",
                "/\n"
                "  Content-Type: text/html; charset=UTF-8\n"
                "/index.html\n"
                "  Content-Type: text/html; charset=UTF-8\n"
            )
        
        zip_buffer.seek(0)
        zip_binary_data = zip_buffer.getvalue()

        logger.info("正在發送 M3 HTML 至 Netlify API...")
        site_id = ""
        if existing_page_url:
            site_id = get_netlify_site_id_from_url(existing_page_url)
            if not site_id:
                return ""
        elif not force_new_site:
            site_id = NETLIFY_SITE_ID
        site_id = site_id or create_netlify_site()
        if not site_id:
            return ""

        deploy = create_netlify_zip_deploy(site_id, zip_binary_data)
        if not deploy:
            return ""

        ready_deploy = wait_for_netlify_deploy_ready(deploy)
        return get_netlify_public_url(ready_deploy or deploy)
    except Exception as e:
        logger.exception(f"deploy_html_to_netlify Exception: {e}")
        return ""

def get_netlify_headers(content_type: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {NETLIFY_AUTH_TOKEN}",
        "Content-Type": content_type,
        "User-Agent": "line-travel-bot"
    }

def get_netlify_site_id_from_url(page_url: str) -> str:
    parsed_url = urlparse(page_url.strip())
    domain = parsed_url.netloc.lower()
    if not domain:
        logger.error(f"無法從網址取得 Netlify domain: {page_url}")
        return ""

    response = requests.get(
        f"https://api.netlify.com/api/v1/sites/{quote(domain, safe='')}",
        headers=get_netlify_headers(),
        timeout=15,
    )

    if response.status_code != 200:
        logger.error(f"Netlify get site error: {response.status_code} - {response.text}")
        return ""

    site = response.json()
    site_id = site.get("id")
    if not site_id:
        logger.error(f"Netlify get site response missing id: {site}")
        return ""
    return site_id

def create_netlify_site() -> str:
    site_name = f"line-travel-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    if NETLIFY_ACCOUNT_SLUG:
        url = f"https://api.netlify.com/api/v1/{NETLIFY_ACCOUNT_SLUG}/sites"
    else:
        url = "https://api.netlify.com/api/v1/sites"

    response = requests.post(
        url,
        headers=get_netlify_headers(),
        json={"name": site_name},
        timeout=15,
    )

    if response.status_code not in [200, 201]:
        logger.error(f"Netlify create site error: {response.status_code} - {response.text}")
        return ""

    site = response.json()
    site_id = site.get("id")
    if not site_id:
        logger.error(f"Netlify create site response missing id: {site}")
        return ""
    return site_id

def create_netlify_zip_deploy(site_id: str, zip_binary_data: bytes) -> dict:
    url = f"https://api.netlify.com/api/v1/sites/{site_id}/deploys"
    response = requests.post(
        url,
        headers=get_netlify_headers("application/zip"),
        data=zip_binary_data,
        timeout=30,
    )

    if response.status_code not in [200, 201]:
        logger.error(f"Netlify deploy error: {response.status_code} - {response.text}")
        return {}

    return response.json()

def wait_for_netlify_deploy_ready(deploy: dict) -> dict:
    deploy_id = deploy.get("id")
    if not deploy_id:
        logger.warning(f"Netlify deploy response missing id: {deploy}")
        return deploy

    deadline = time.time() + NETLIFY_DEPLOY_POLL_SECONDS
    while time.time() < deadline:
        state = deploy.get("state")
        if state == "ready":
            return deploy
        if state == "error":
            logger.error(f"Netlify deploy failed: {deploy}")
            return {}

        time.sleep(2)
        response = requests.get(
            f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
            headers=get_netlify_headers(),
            timeout=15,
        )
        if response.status_code != 200:
            logger.error(f"Netlify deploy poll error: {response.status_code} - {response.text}")
            return deploy
        deploy = response.json()

    logger.warning(f"Netlify deploy was not ready within {NETLIFY_DEPLOY_POLL_SECONDS}s: {deploy}")
    return deploy

def get_netlify_public_url(deploy: dict) -> str:
    for key in ("ssl_url", "deploy_ssl_url", "url", "deploy_url"):
        public_url = deploy.get(key)
        if public_url:
            return public_url.replace("http://", "https://", 1)

    logger.error(f"Netlify response missing public URL: {deploy}")
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
