import os
import re
import uuid
import html
import logging
import tempfile
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
    WebAppInfo,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
)

# PIL uchun importlar
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import sys

# =========================
# LOGGING SOZLAMALARI
# =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# SOZLAMALAR
# =========================
DIRECTOR_ID = 934386169
SHEET_ID = "108hVJMPQNTYfrdUV1VOFXgi_v144jev0DeZiaUm4How"

RANKING_SHEET = "Ranking"
PENDING_SHEET = "Pending"
HISTORY_SHEET = "History"

INITIAL_RATING = 1000.0
K_FACTOR = 24.0

TOKEN = os.environ.get("TELEGRAM_TOKEN")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

# TOKEN va BASE_URL ni tekshirish
if not TOKEN or not BASE_URL:
    raise ValueError("❌ TOKEN yoki BASE_URL topilmadi! Iltimos, muhit o'zgaruvchilarini tekshiring.")

# =========================
# KESH SOZLAMALARI
# =========================
CACHE_TTL = 30  # 30 soniya
ranking_cache = {"data": None, "timestamp": 0}

# =========================
# TELEGRAM UPDATER / DISPATCHER
# =========================
updater = Updater(TOKEN, use_context=True)
bot = updater.bot
dispatcher = updater.dispatcher

# =========================
# GOOGLE SHEETS
# =========================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

try:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    logger.info("✅ Google Sheets ga ulanish muvaffaqiyatli")
except Exception as e:
    logger.error(f"❌ Google Sheets ga ulanishda xatolik: {e}")
    raise

def get_or_create_worksheet(title: str, rows: int = 2000, cols: int = 20):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))

ranking_ws = get_or_create_worksheet(RANKING_SHEET)
pending_ws = get_or_create_worksheet(PENDING_SHEET)
history_ws = get_or_create_worksheet(HISTORY_SHEET)

RANKING_HEADERS = [
    "Ism", "Oyinlar", "Galaba", "Durang", "Maglubiyat",
    "UrganGoli", "OtkazganGoli", "Achko", "Streak",
    "OxirgiNatija", "UpdatedAt"
]

PENDING_HEADERS = [
    "ID", "Player1", "Score1", "Score2", "Player2",
    "SubmittedByID", "SubmittedByName", "ChatID", "ChatTitle",
    "Status", "CreatedAt", "ApprovalMessageID"
]

HISTORY_HEADERS = [
    "ID", "Player1", "Score1", "Score2", "Player2",
    "SubmittedByName", "ApprovedByID", "ApprovedAt",
    "Delta1", "Delta2", "OldRating1", "NewRating1",
    "OldRating2", "NewRating2"
]

def ensure_headers(ws, headers):
    row1 = ws.row_values(1)
    if row1 != headers:
        ws.clear()
        ws.append_row(headers)

ensure_headers(ranking_ws, RANKING_HEADERS)
ensure_headers(pending_ws, PENDING_HEADERS)
ensure_headers(history_ws, HISTORY_HEADERS)

# =========================
# YORDAMCHI FUNKSIYALAR
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def esc(text: str) -> str:
    return html.escape(str(text))

def normalize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip())
    if not name:
        return name
    return " ".join(word[:1].upper() + word[1:].lower() for word in name.split())

def safe_int(v, default=0):
    try:
        return int(float(str(v).strip().replace(",", ".")))
    except Exception:
        return default

def safe_float(v, default=0.0):
    try:
        s = str(v).strip().replace(",", ".")
        if s == "":
            return default
        return float(s)
    except Exception:
        return default

def get_reply_menu():
    return ReplyKeyboardMarkup(
        [
            ["📊 Jadval", "🥇 Top 3"],
            ["📋 Menyu", "ℹ️ Qoida"],
        ],
        resize_keyboard=True,
    )

def is_director(user_id: int) -> bool:
    return user_id == DIRECTOR_ID

def parse_score_message(text: str):
    text = text.strip()
    text = re.sub(r"\s+", " ", text)

    pattern = r"^(.+?)\s+(\d+)\s*[-:]\s*(\d+)\s+(.+)$"
    m = re.match(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None

    p1 = normalize_name(m.group(1))
    s1 = int(m.group(2))
    s2 = int(m.group(3))
    p2 = normalize_name(m.group(4))

    if not p1 or not p2:
        return None
    if p1.lower() == p2.lower():
        return None
    if s1 > 20 or s2 > 20:  # Cheklov
        return None
    if len(p1) < 2 or len(p2) < 2:  # Ismlar juda qisqa bo'lmasin
        return None

    return p1, s1, s2, p2

def sheet_rows(ws, headers):
    try:
        values = ws.get_all_values()
        result = []
        for idx, row in enumerate(values[1:], start=2):
            row = row + [""] * (len(headers) - len(row))
            item = dict(zip(headers, row[:len(headers)]))
            if any(str(v).strip() for v in item.values()):
                result.append((idx, item))
        return result
    except Exception as e:
        logger.error(f"Sheet o'qishda xatolik: {e}")
        return []

def get_cached_ranking():
    """Kesh bilan ranking ma'lumotlarini olish"""
    global ranking_cache
    current_time = datetime.now().timestamp()
    
    if current_time - ranking_cache["timestamp"] > CACHE_TTL:
        ranking_cache["data"] = get_sorted_ranking()
        ranking_cache["timestamp"] = current_time
        logger.info("🔄 Ranking kesh yangilandi")
    
    return ranking_cache["data"]

def ranking_records():
    return sheet_rows(ranking_ws, RANKING_HEADERS)

def pending_records():
    return sheet_rows(pending_ws, PENDING_HEADERS)

def find_ranking_row(name: str):
    for idx, row in ranking_records():
        if str(row["Ism"]).strip().lower() == name.strip().lower():
            return idx, row
    return None, None

def create_player_if_missing(name: str):
    row_idx, row = find_ranking_row(name)
    if row_idx:
        return row_idx, row

    try:
        ranking_ws.append_row([
            name, 0, 0, 0, 0, 0, 0, INITIAL_RATING, 0, "-", now_str()
        ])
        logger.info(f"✅ Yangi o'yinchi qo'shildi: {name}")
    except Exception as e:
        logger.error(f"O'yinchi qo'shishda xatolik: {e}")
    
    return find_ranking_row(name)

def expected_score(r1: float, r2: float) -> float:
    return 1 / (1 + 10 ** ((r2 - r1) / 400))

def calc_elo_change(r1: float, r2: float, score1: int, score2: int, games1: int = 0, games2: int = 0):
    """ELO o'zgarishini hisoblash, yangi o'yinchilar uchun K faktor kattaroq"""
    e1 = expected_score(r1, r2)
    e2 = expected_score(r2, r1)

    if score1 > score2:
        s1, s2 = 1.0, 0.0
    elif score1 < score2:
        s1, s2 = 0.0, 1.0
    else:
        s1, s2 = 0.5, 0.5

    goal_diff = abs(score1 - score2)
    bonus = min(3, max(0, goal_diff - 1))

    # Yangi o'yinchilar uchun K faktor kattaroq
    k1 = 32.0 if games1 < 10 else K_FACTOR
    k2 = 32.0 if games2 < 10 else K_FACTOR

    delta1 = k1 * (s1 - e1)
    delta2 = k2 * (s2 - e2)

    if s1 == 1.0:
        delta1 += bonus
        delta2 -= bonus
    elif s2 == 1.0:
        delta2 += bonus
        delta1 -= bonus

    if s1 == 1.0 and delta1 < 4:
        delta1, delta2 = 4, -4
    elif s2 == 1.0 and delta2 < 4:
        delta2, delta1 = 4, -4
    elif s1 == 0.5:
        if r1 < r2 and delta1 < 2:
            delta1, delta2 = 2, -2
        elif r2 < r1 and delta2 < 2:
            delta2, delta1 = 2, -2

    return round(delta1, 2), round(delta2, 2)

def update_player_stats(name: str, goals_for: int, goals_against: int, result: str, delta_rating: float):
    row_idx, row = create_player_if_missing(name)

    games = safe_int(row["Oyinlar"]) + 1
    wins = safe_int(row["Galaba"])
    draws = safe_int(row["Durang"])
    losses = safe_int(row["Maglubiyat"])
    gf = safe_int(row["UrganGoli"]) + goals_for
    ga = safe_int(row["OtkazganGoli"]) + goals_against
    rating = safe_float(row["Achko"], INITIAL_RATING) + float(delta_rating)
    streak = safe_int(row["Streak"])

    if result == "W":
        wins += 1
        streak = streak + 1 if streak >= 0 else 1
        last_result = "G"
    elif result == "D":
        draws += 1
        streak = 0
        last_result = "D"
    else:
        losses += 1
        streak = streak - 1 if streak <= 0 else -1
        last_result = "M"

    try:
        ranking_ws.update(
            f"A{row_idx}:K{row_idx}",
            [[
                name, games, wins, draws, losses,
                gf, ga, round(rating, 2), streak, last_result, now_str()
            ]]
        )
        # Keshni tozalash
        global ranking_cache
        ranking_cache["data"] = None
        ranking_cache["timestamp"] = 0
    except Exception as e:
        logger.error(f"O'yinchi statistikasini yangilashda xatolik: {e}")

def get_sorted_ranking():
    rows = [row for _, row in ranking_records()]

    cleaned = []
    for row in rows:
        name = str(row.get("Ism", "")).strip()
        if not name:
            continue

        cleaned.append({
            "Ism": name,
            "Oyinlar": safe_int(row.get("Oyinlar")),
            "Galaba": safe_int(row.get("Galaba")),
            "Durang": safe_int(row.get("Durang")),
            "Maglubiyat": safe_int(row.get("Maglubiyat")),
            "UrganGoli": safe_int(row.get("UrganGoli")),
            "OtkazganGoli": safe_int(row.get("OtkazganGoli")),
            "Achko": safe_float(row.get("Achko"), INITIAL_RATING),
            "Streak": safe_int(row.get("Streak")),
            "OxirgiNatija": str(row.get("OxirgiNatija", "")).strip(),
            "UpdatedAt": str(row.get("UpdatedAt", "")).strip(),
        })

    cleaned.sort(
        key=lambda x: (
            x["Achko"],
            x["Galaba"],
            x["UrganGoli"] - x["OtkazganGoli"],
            x["UrganGoli"],
        ),
        reverse=True
    )
    return cleaned

def format_top_banner(rows):
    if not rows:
        return (
            "🏆 <b>EFOOTBALL PC REYTING BOT</b>\n\n"
            "👑 <b>Chempion:</b> Hali yo'q\n"
            "⭐ <b>Achko:</b> -"
        )

    top = rows[0]
    return (
        "🏆 <b>EFOOTBALL PC REYTING BOT</b>\n\n"
        f"👑 <b>Chempion:</b> {esc(top['Ism'])}\n"
        f"⭐ <b>Achko:</b> {safe_float(top['Achko']):.2f}\n"
        f"🎮 <b>O'yin:</b> {top['Oyinlar']} | ✅ {top['Galaba']} | 🤝 {top['Durang']} | ❌ {top['Maglubiyat']}\n"
        f"⚽ <b>Gollar:</b> {top['UrganGoli']}-{top['OtkazganGoli']}"
    )

def format_top3():
    rows = get_sorted_ranking()
    if not rows:
        return "🏅 TOP 3\n\nHali reyting yo'q."

    lines = ["🏅 <b>TOP 3</b>", ""]
    medals = ["👑", "🥈", "🥉"]
    faces = ["😎", "🎮", "⚽"]

    for i, row in enumerate(rows[:3], start=1):
        lines.append(
            f"{medals[i-1]} <b>{i}. {esc(row['Ism'])}</b> — ⭐ {safe_float(row['Achko']):.2f} | "
            f"{faces[i-1]} {row['Oyinlar']} | ✅ {row['Galaba']} | ⚽ {row['UrganGoli']}-{row['OtkazganGoli']}"
        )

    return "\n".join(lines)

def format_table():
    rows = get_sorted_ranking()
    if not rows:
        return "🏆 <b>EFOOTBALL PC REYTING JADVALI</b>\n\nHali reytingda o'yinchi yo'q."

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    sep = "┄" * 22
    lines = ["🏆 <b>EFOOTBALL PC REYTING JADVALI</b>"]

    for i, r in enumerate(rows, start=1):
        medal = medals.get(i, f"{i}.")
        nm    = esc(str(r["Ism"]))
        ac    = safe_float(r["Achko"])
        o     = r["Oyinlar"]
        g     = r["Galaba"]
        d     = r["Durang"]
        m     = r["Maglubiyat"]
        gl    = f"{r['UrganGoli']}-{r['OtkazganGoli']}"
        lines.append(sep)
        lines.append(f"{medal} <b>{nm}</b>  ⭐ {ac:.0f}")
        lines.append(f"🎮{o} ✅{g} 🤝{d} ❌{m} ⚽{gl}")

    lines.append(sep)
    return "\n".join(lines)

def format_menu_text():
    return (
        "📋 <b>Bot menyusi</b>\n\n"
        "Natija yuborish:\n"
        "<code>Ali 3-2 Vali</code>\n\n"
        "Komandalar:\n"
        "/start - Boshlash\n"
        "/menu - Menyu\n"
        "/table - To'liq jadval\n"
        "/top3 - Top 3\n"
        "/pending - Kutilayotgan natijalar\n"
        "/reset - Reytingni tozalash (Admin)\n"
        "/restart - Botni qayta ishga tushirish (Admin)\n"
        "/help - Qoidalar"
    )

def format_help_text():
    return (
        "ℹ️ <b>Qoidalar</b>\n\n"
        "1) Guruhdagi istalgan odam natija yuborishi mumkin.\n"
        "2) Natija darrov hisoblanmaydi.\n"
        "3) Tasdiqlash faqat <b>Admin</b> tomonidan bo'ladi.\n"
        "4) Achko ELOga o'xshash hisoblanadi.\n"
        "5) To'g'ri format:\n"
        "<code>Ali 4-3 Vali</code>"
    )

def add_pending_result(p1, s1, s2, p2, submitted_by_id, submitted_by_name, chat_id, chat_title):
    pending_id = str(uuid.uuid4())[:8]
    try:
        pending_ws.append_row([
            pending_id, p1, s1, s2, p2,
            submitted_by_id, submitted_by_name, chat_id, chat_title,
            "PENDING", now_str(), ""
        ])
        logger.info(f"✅ Yangi pending natija: {pending_id} - {p1} {s1}-{s2} {p2}")
    except Exception as e:
        logger.error(f"Pending qo'shishda xatolik: {e}")
    return pending_id

def find_pending_row(pending_id: str):
    for idx, row in pending_records():
        if str(row["ID"]).strip() == pending_id:
            return idx, row
    return None, None

def set_pending_status(pending_id: str, status: str, message_id=None):
    row_idx, row = find_pending_row(pending_id)
    if not row_idx:
        return False

    approval_message_id = row["ApprovalMessageID"]
    if message_id is not None:
        approval_message_id = str(message_id)

    try:
        pending_ws.update(
            f"A{row_idx}:L{row_idx}",
            [[
                row["ID"], row["Player1"], row["Score1"], row["Score2"], row["Player2"],
                row["SubmittedByID"], row["SubmittedByName"], row["ChatID"], row["ChatTitle"],
                status, row["CreatedAt"], approval_message_id
            ]]
        )
        logger.info(f"✅ Pending status yangilandi: {pending_id} -> {status}")
        return True
    except Exception as e:
        logger.error(f"Pending status yangilashda xatolik: {e}")
        return False

def apply_approved_result(pending_row, approver_id):
    p1 = normalize_name(str(pending_row["Player1"]))
    p2 = normalize_name(str(pending_row["Player2"]))
    s1 = safe_int(pending_row["Score1"])
    s2 = safe_int(pending_row["Score2"])

    _, row1 = create_player_if_missing(p1)
    _, row2 = create_player_if_missing(p2)

    old1 = safe_float(row1["Achko"], INITIAL_RATING)
    old2 = safe_float(row2["Achko"], INITIAL_RATING)
    
    games1 = safe_int(row1["Oyinlar"])
    games2 = safe_int(row2["Oyinlar"])

    delta1, delta2 = calc_elo_change(old1, old2, s1, s2, games1, games2)

    if s1 > s2:
        res1, res2 = "W", "L"
    elif s1 < s2:
        res1, res2 = "L", "W"
    else:
        res1 = res2 = "D"

    update_player_stats(p1, s1, s2, res1, delta1)
    update_player_stats(p2, s2, s1, res2, delta2)

    try:
        history_ws.append_row([
            pending_row["ID"], p1, s1, s2, p2,
            pending_row["SubmittedByName"], approver_id, now_str(),
            delta1, delta2, old1, round(old1 + delta1, 2), old2, round(old2 + delta2, 2)
        ])
        logger.info(f"✅ History ga qo'shildi: {pending_row['ID']}")
    except Exception as e:
        logger.error(f"History ga qo'shishda xatolik: {e}")

    set_pending_status(pending_row["ID"], "APPROVED")
    return delta1, delta2

# =========================
# RASM YARATISH (PIL)
# =========================
class RankingImageGenerator:
    def __init__(self, size="telegram"):
        """Rasm o'lchamlari"""
        self.sizes = {
            "4k": (3840, 2160),
            "2k": (2560, 1440),
            "hd": (1920, 1080),
            "telegram": (1280, 720)
        }
        self.WIDTH, self.HEIGHT = self.sizes.get(size, self.sizes["telegram"])
        
        # Ranglar
        self.BG_COLOR = (3, 8, 20)
        self.NEON_BLUE = (0, 180, 255)
        self.NEON_GOLD = (255, 196, 0)
        self.NEON_SILVER = (220, 220, 220)
        self.NEON_BRONZE = (205, 127, 50)
        self.WHITE = (255, 255, 255)
        
        # Shriftlarni yuklash
        self.fonts = self._load_fonts()
    
    def _load_fonts(self):
        """Shriftlarni topish va yuklash"""
        font_paths = {
            "bold": [
                "arialbd.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "/System/Library/Fonts/Arial Bold.ttf",
                "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            ],
            "regular": [
                "arial.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "/System/Library/Fonts/Arial.ttf",
                "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
            ]
        }
        
        fonts = {}
        for name, paths in font_paths.items():
            found = False
            for path in paths:
                try:
                    if os.path.exists(path):
                        fonts[name] = path
                        found = True
                        break
                except:
                    continue
            if not found:
                fonts[name] = None
                logger.warning(f"⚠️ {name} shrift topilmadi")
        
        return fonts
    
    def _get_font(self, font_type, size):
        """Shrift obyektini olish"""
        try:
            if self.fonts.get(font_type) and os.path.exists(self.fonts[font_type]):
                return ImageFont.truetype(self.fonts[font_type], size)
            else:
                return ImageFont.load_default()
        except:
            return ImageFont.load_default()
    
    def draw_glow_text(self, base, pos, text, font_size, color, centered=False):
        """Glow effektli matn chizish"""
        font = self._get_font("bold", font_size)
        
        # Agar markazlashtirish kerak bo'lsa
        if centered:
            try:
                bbox = font.getbbox(text)
                text_width = bbox[2] - bbox[0]
                pos = (pos[0] - text_width // 2, pos[1])
            except:
                pass
        
        # Glow qatlami
        glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        
        # Bir necha qatlam glow
        for r in [20, 12, 6]:
            gdraw.text(pos, text, font=font, fill=color + (60,))
            glow = glow.filter(ImageFilter.GaussianBlur(r))
        
        base.alpha_composite(glow)
        
        # Asosiy matn
        draw = ImageDraw.Draw(base)
        draw.text(pos, text, font=font, fill=color + (255,))
    
    def draw_glow_line(self, base, xy, color):
        """Glow effektli chiziq"""
        glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        
        gdraw.line(xy, fill=color + (255,), width=3)
        
        for blur in [20, 12, 6]:
            glow = glow.filter(ImageFilter.GaussianBlur(blur))
        
        base.alpha_composite(glow)
        
        draw = ImageDraw.Draw(base)
        draw.line(xy, fill=color + (255,), width=2)
    
    def generate(self, rows: List[Dict]) -> Optional[str]:
        """Rasm yaratish"""
        if not rows:
            return self._generate_empty()
        
        # Rasm o'lchamlari
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        # Fon gradienti
        for y in range(self.HEIGHT):
            c = int(10 + (y / self.HEIGHT) * 30)
            draw.line((0, y, self.WIDTH, y), fill=(0, 0, c))
        
        # Sarlavha
        title_size = min(120, int(self.WIDTH / 15))
        self.draw_glow_text(
            img,
            (self.WIDTH // 2, int(self.HEIGHT * 0.05)),
            "⚽ EFOOTBALL PC REYTING",
            title_size,
            self.NEON_BLUE,
            centered=True
        )
        
        # Jadval chegarasi
        margin = int(self.WIDTH * 0.03)
        top_margin = int(self.HEIGHT * 0.15)
        bottom_margin = int(self.HEIGHT * 0.05)
        
        # Asosiy ramka
        frame = Image.new("RGBA", img.size, (0, 0, 0, 0))
        fdraw = ImageDraw.Draw(frame)
        fdraw.rounded_rectangle(
            (margin, top_margin, self.WIDTH - margin, self.HEIGHT - bottom_margin),
            radius=min(25, int(self.WIDTH * 0.01)),
            outline=self.NEON_BLUE + (255,),
            width=3
        )
        
        # Glow effekt
        for blur in [30, 15, 8]:
            frame = frame.filter(ImageFilter.GaussianBlur(blur))
        img.alpha_composite(frame)
        
        # Sarlavha ostidagi chiziq
        line_y = top_margin + int(self.HEIGHT * 0.06)
        self.draw_glow_line(
            img,
            (margin + 20, line_y, self.WIDTH - margin - 20, line_y),
            self.NEON_BLUE
        )
        
        # Sarlavhalar
        headers = [
            ("№", int(self.WIDTH * 0.04)),
            ("O'YINCHI", int(self.WIDTH * 0.18)),
            ("O'", int(self.WIDTH * 0.05)),
            ("G'", int(self.WIDTH * 0.05)),
            ("D", int(self.WIDTH * 0.05)),
            ("M", int(self.WIDTH * 0.05)),
            ("GOL", int(self.WIDTH * 0.09)),
            ("ACHKO", int(self.WIDTH * 0.12)),
        ]
        
        header_y = top_margin + int(self.HEIGHT * 0.09)
        x = margin + int(self.WIDTH * 0.02)
        
        for title, width in headers:
            self.draw_glow_text(
                img,
                (x, header_y),
                title,
                int(self.WIDTH * 0.04),
                self.NEON_BLUE
            )
            x += width
        
        # Ma'lumotlar
        row_y = top_margin + int(self.HEIGHT * 0.16)
        row_height = int(self.HEIGHT * 0.075)
        max_rows = min(20, len(rows))
        
        medals = {
            1: ("🥇", self.NEON_GOLD),
            2: ("🥈", self.NEON_SILVER),
            3: ("🥉", self.NEON_BRONZE)
        }
        
        for idx in range(max_rows):
            row = rows[idx]
            pos = idx + 1
            
            # Satr oralig'i
            if pos > 1:
                self.draw_glow_line(
                    img,
                    (margin + 20, row_y - 10, self.WIDTH - margin - 20, row_y - 10),
                    (40, 80, 150)
                )
            
            # Medal yoki raqam
            if pos in medals:
                rank_text, rank_color = medals[pos]
            else:
                rank_text = str(pos)
                rank_color = self.WHITE
            
            # Ma'lumotlar
            values = [
                rank_text,
                row["Ism"],
                str(row["Oyinlar"]),
                str(row["Galaba"]),
                str(row["Durang"]),
                str(row["Maglubiyat"]),
                f"{row['UrganGoli']}-{row['OtkazganGoli']}",
                f"{float(row['Achko']):.0f}"
            ]
            
            colors = [
                rank_color,
                self.WHITE,
                self.WHITE,
                self.WHITE,
                self.WHITE,
                self.WHITE,
                self.NEON_BLUE,
                self.NEON_GOLD
            ]
            
            x = margin + int(self.WIDTH * 0.02)
            font_size = int(self.WIDTH * 0.035)
            
            for i, value in enumerate(values):
                # Ismni qisqartirish
                if i == 1 and len(value) > 20:
                    value = value[:18] + "..."
                
                self.draw_glow_text(
                    img,
                    (x, row_y),
                    str(value),
                    font_size,
                    colors[i]
                )
                x += headers[i][1]
            
            row_y += row_height
        
        # Pastki qismdagi sana
        date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        self.draw_glow_text(
            img,
            (self.WIDTH - margin - int(self.WIDTH * 0.15), self.HEIGHT - int(self.HEIGHT * 0.02)),
            f"📅 {date_str}",
            int(self.WIDTH * 0.025),
            (100, 150, 200)
        )
        
        # Faylni vaqtinchalik saqlash
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name, quality=85, optimize=True)
                logger.info(f"✅ Rasm yaratildi: {tmp.name}")
                return tmp.name
        except Exception as e:
            logger.error(f"❌ Rasm saqlashda xatolik: {e}")
            return None
    
    def _generate_empty(self) -> Optional[str]:
        """Bo'sh jadval rasmi"""
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), self.BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        # Fon gradienti
        for y in range(self.HEIGHT):
            c = int(10 + (y / self.HEIGHT) * 30)
            draw.line((0, y, self.WIDTH, y), fill=(0, 0, c))
        
        # Xabar
        self.draw_glow_text(
            img,
            (self.WIDTH // 2, self.HEIGHT // 2 - 50),
            "📊 REYTING YO'Q",
            int(self.WIDTH * 0.06),
            self.NEON_BLUE,
            centered=True
        )
        
        self.draw_glow_text(
            img,
            (self.WIDTH // 2, self.HEIGHT // 2 + 50),
            "Hali hech qanday o'yin o'tkazilmagan",
            int(self.WIDTH * 0.03),
            (100, 150, 200),
            centered=True
        )
        
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name, quality=85, optimize=True)
                return tmp.name
        except Exception as e:
            logger.error(f"❌ Bo'sh rasm saqlashda xatolik: {e}")
            return None

# =========================
# KOMANDALAR
# =========================
def set_bot_commands(bot_obj):
    commands = [
        BotCommand("start", "Boshlash"),
        BotCommand("menu", "Menyu"),
        BotCommand("table", "Reyting jadvali"),
        BotCommand("top3", "Top 3"),
        BotCommand("pending", "Kutilayotgan natijalar"),
        BotCommand("reset", "Reytingni tozalash"),
        BotCommand("restart", "Botni qayta ishga tushirish"),
        BotCommand("help", "Qoidalar"),
    ]
    try:
        bot_obj.set_my_commands(commands)
        logger.info("✅ Bot komandalari o'rnatildi")
    except Exception as e:
        logger.error(f"❌ Komandalarni o'rnatishda xatolik: {e}")

def start(update: Update, context: CallbackContext):
    rows = get_cached_ranking()
    text = format_top_banner(rows) + "\n\n" + (
        "👋 <b>eFootball Reyting botiga xush kelibsiz!</b>\n\n"
        "Natija yuborish formati:\n"
        "<code>Ali 3-2 Vali</code>"
    )
    update.message.reply_text(text, parse_mode="HTML", reply_markup=get_reply_menu())
    logger.info(f"📝 Start komandasi: {update.effective_user.full_name}")

def menu_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_menu_text(), parse_mode="HTML", reply_markup=get_reply_menu())

def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_help_text(), parse_mode="HTML", reply_markup=get_reply_menu())

def table_cmd(update: Update, context: CallbackContext):
    """Jadvalni rasm sifatida yuborish"""
    try:
        # Kesh dan ma'lumotlarni olish
        rows = get_cached_ranking()
        
        # Rasm yaratish
        generator = RankingImageGenerator("telegram")
        image_path = generator.generate(rows)
        
        if image_path and os.path.exists(image_path):
            # Rasmni yuborish
            with open(image_path, 'rb') as f:
                update.message.reply_photo(
                    photo=f,
                    caption="📊 <b>EFOOTBALL PC REYTING JADVALI</b>",
                    parse_mode="HTML",
                    reply_markup=get_reply_menu()
                )
            # Vaqtinchalik faylni o'chirish
            try:
                os.unlink(image_path)
            except:
                pass
            logger.info(f"✅ Jadval rasmi yuborildi: {update.effective_user.full_name}")
        else:
            # Agar rasm yaratilmagan bo'lsa, matnli versiyani yuborish
            update.message.reply_text(
                format_table(),
                parse_mode="HTML",
                reply_markup=get_reply_menu()
            )
            logger.warning("⚠️ Rasm yaratilmadi, matnli versiya yuborildi")
            
    except Exception as e:
        logger.error(f"❌ Jadval rasmini yuborishda xatolik: {e}")
        # Xatolik bo'lsa matnli versiyani yuborish
        update.message.reply_text(
            format_table(),
            parse_mode="HTML",
            reply_markup=get_reply_menu()
        )

def top3_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_top3(), parse_mode="HTML", reply_markup=get_reply_menu())

def pending_cmd(update: Update, context: CallbackContext):
    if not is_director(update.effective_user.id):
        update.message.reply_text("⛔ Bu bo'lim faqat admin uchun.")
        return

    rows = [row for _, row in pending_records() if str(row["Status"]).upper() == "PENDING"]
    if not rows:
        update.message.reply_text("✅ Kutilayotgan natija yo'q.")
        return

    lines = ["⏳ <b>Kutilayotgan natijalar</b>", ""]
    for r in rows[-10:]:
        lines.append(
            f"• <b>{esc(r['ID'])}</b> — {esc(r['Player1'])} {r['Score1']}-{r['Score2']} {esc(r['Player2'])} "
            f"({esc(r['SubmittedByName'])})"
        )
    update.message.reply_text("\n".join(lines), parse_mode="HTML")

def reset_cmd(update: Update, context: CallbackContext):
    if not is_director(update.effective_user.id):
        update.message.reply_text("⛔ /reset faqat admin uchun.")
        return

    try:
        ranking_ws.clear()
        pending_ws.clear()
        history_ws.clear()

        ensure_headers(ranking_ws, RANKING_HEADERS)
        ensure_headers(pending_ws, PENDING_HEADERS)
        ensure_headers(history_ws, HISTORY_HEADERS)
        
        # Keshni tozalash
        global ranking_cache
        ranking_cache["data"] = None
        ranking_cache["timestamp"] = 0

        update.message.reply_text("✅ Reyting, pending va history tozalandi.")
        logger.info(f"🗑️ Reyting tozalandi: {update.effective_user.full_name}")
    except Exception as e:
        logger.error(f"❌ Reset qilishda xatolik: {e}")
        update.message.reply_text("❌ Reytingni tozalashda xatolik yuz berdi.")

def restart_cmd(update: Update, context: CallbackContext):
    if not is_director(update.effective_user.id):
        update.message.reply_text("⛔ /restart faqat admin uchun.")
        return

    update.message.reply_text("🔄 Bot qayta ishga tushirilmoqda...")
    logger.info(f"🔄 Bot qayta ishga tushirilmoqda: {update.effective_user.full_name}")
    os.execl(sys.executable, sys.executable, *sys.argv)

def handle_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    user = query.from_user
    query.answer()

    try:
        data = query.data or ""
        if ":" not in data:
            return

        action, pending_id = data.split(":", 1)

        if not is_director(user.id):
            query.answer("Faqat admin tasdiqlay oladi.", show_alert=True)
            return

        _, row = find_pending_row(pending_id)
        if not row:
            query.edit_message_text("❌ Bu pending natija topilmadi.")
            return

        status = str(row["Status"]).upper()
        if status != "PENDING":
            query.answer("Bu natija allaqachon ko'rib chiqilgan.", show_alert=True)
            return

        p1 = esc(row["Player1"])
        p2 = esc(row["Player2"])
        s1 = row["Score1"]
        s2 = row["Score2"]

        if action == "approve":
            delta1, delta2 = apply_approved_result(row, user.id)
            query.edit_message_text(
                f"✅ <b>Admin tasdiqladi</b>\n\n"
                f"{p1} {s1}-{s2} {p2}\n\n"
                f"⭐ {p1}: {delta1:+.2f}\n"
                f"⭐ {p2}: {delta2:+.2f}",
                parse_mode="HTML",
            )
            # Top3 ni yangilab yuborish
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=format_top3(),
                parse_mode="HTML"
            )
            logger.info(f"✅ Natija tasdiqlandi: {pending_id} - {user.full_name}")

        elif action == "reject":
            set_pending_status(pending_id, "REJECTED")
            query.edit_message_text(
                f"❌ <b>Admin rad etdi</b>\n\n{p1} {s1}-{s2} {p2}",
                parse_mode="HTML",
            )
            logger.info(f"❌ Natija rad etildi: {pending_id} - {user.full_name}")

    except Exception as e:
        logger.error(f"❌ Button xatoligi: {e}")
        try:
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Tasdiqlashda xatolik:\n<code>{esc(str(e))}</code>",
                parse_mode="HTML",
            )
        except:
            pass

def handle_menu_buttons_text(update: Update, context: CallbackContext):
    text = update.message.text.strip()

    if text == "📊 Jadval":
        return table_cmd(update, context)
    if text == "🥇 Top 3":
        return top3_cmd(update, context)
    if text == "📋 Menyu":
        return menu_cmd(update, context)
    if text == "ℹ️ Qoida":
        return help_cmd(update, context)

    parsed = parse_score_message(text)
    if not parsed:
        return

    p1, s1, s2, p2 = parsed
    submitted_by = update.effective_user
    chat = update.effective_chat

    pending_id = add_pending_result(
        p1, s1, s2, p2,
        submitted_by.id,
        submitted_by.full_name,
        chat.id,
        getattr(chat, "title", "") or "Private",
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve:{pending_id}"),
            InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject:{pending_id}"),
        ]
    ])

    update.message.reply_text(
        "⏳ <b>Natija qabul qilindi</b>\n\n"
        f"🆔 <b>{esc(pending_id)}</b>\n"
        f"{esc(p1)} {s1}-{s2} {esc(p2)}\n\n"
        "Admin tasdiqlashi kutilmoqda.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    logger.info(f"📝 Yangi natija yuborildi: {p1} {s1}-{s2} {p2} - {submitted_by.full_name}")

# =========================
# HANDLERLAR RO'YXATI
# =========================
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("menu", menu_cmd))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(CommandHandler("table", table_cmd))
dispatcher.add_handler(CommandHandler("top3", top3_cmd))
dispatcher.add_handler(CommandHandler("pending", pending_cmd))
dispatcher.add_handler(CommandHandler("reset", reset_cmd))
dispatcher.add_handler(CommandHandler("restart", restart_cmd))
dispatcher.add_handler(CallbackQueryHandler(handle_buttons))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_menu_buttons_text))

# =========================
# WEBHOOK / FLASK
# =========================
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "EFOOTBALL PC bot webhook is running", 200

@app.route("/webapp", methods=["GET"])
def webapp():
    rows = get_cached_ranking()
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    rows_html = ""
    for i, row in enumerate(rows, start=1):
        medal = medals.get(i, "")
        name  = html.escape(str(row["Ism"]))
        achko = safe_float(row["Achko"])
        o     = row["Oyinlar"]
        g     = row["Galaba"]
        d     = row["Durang"]
        m     = row["Maglubiyat"]
        gol   = f"{row['UrganGoli']}-{row['OtkazganGoli']}"
        streak = safe_int(row.get("Streak", 0))
        streak_str = f"+{streak}" if streak > 0 else str(streak)
        rows_html += f"""
        <tr onclick="showDetail(this)"
            data-name="{name}" data-achko="{achko:.2f}"
            data-o="{o}" data-g="{g}" data-d="{d}" data-m="{m}"
            data-gol="{gol}" data-streak="{streak_str}">
          <td class="num">{medal}{i}</td>
          <td class="name">{name}</td>
          <td>{o}</td>
          <td class="win">{g}</td>
          <td>{d}</td>
          <td class="loss">{m}</td>
          <td>{gol}</td>
          <td class="achko">{achko:.2f}</td>
        </tr>"""

    page = f"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>eFootball Reyting</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0e1117;
    color: #e0e6f0;
    min-height: 100vh;
    padding-bottom: 80px;
  }}
  .header {{
    background: linear-gradient(135deg, #1a2540 0%, #0e1117 100%);
    border-bottom: 2px solid #f0b429;
    padding: 16px;
    text-align: center;
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .header h1 {{
    font-size: 16px;
    font-weight: 700;
    color: #f0b429;
    letter-spacing: 1px;
    text-transform: uppercase;
  }}
  .header p {{
    font-size: 11px;
    color: #7a8aaa;
    margin-top: 2px;
  }}
  .table-wrap {{
    overflow-x: auto;
    padding: 12px 8px;
  }}
  table {{
    width: 100%;
    min-width: 340px;
    border-collapse: collapse;
    font-size: 13px;
  }}
  thead tr {{
    background: #1a2540;
  }}
  thead th {{
    padding: 10px 6px;
    text-align: center;
    color: #7a8aaa;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    border-bottom: 1px solid #2a3550;
    white-space: nowrap;
  }}
  thead th:nth-child(2) {{ text-align: left; padding-left: 8px; }}
  tbody tr {{
    border-bottom: 1px solid #1a2540;
    cursor: pointer;
    transition: background 0.15s;
  }}
  tbody tr:hover, tbody tr.active {{
    background: #1e2d50 !important;
    border-bottom-color: #f0b429;
  }}
  tbody tr:nth-child(odd) {{ background: #12181f; }}
  tbody tr:nth-child(even) {{ background: #0e1117; }}
  tbody tr:first-child {{ background: linear-gradient(90deg, #1a2a10 0%, #12181f 100%); }}
  tbody tr:nth-child(2) {{ background: linear-gradient(90deg, #1a1e2a 0%, #12181f 100%); }}
  tbody tr:nth-child(3) {{ background: linear-gradient(90deg, #1a1510 0%, #12181f 100%); }}
  td {{
    padding: 10px 6px;
    text-align: center;
    white-space: nowrap;
  }}
  td.num {{
    font-size: 15px;
    width: 36px;
  }}
  td.name {{
    text-align: left;
    padding-left: 8px;
    font-weight: 600;
    color: #e8eef8;
    max-width: 110px;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  td.win  {{ color: #4caf7d; font-weight: 600; }}
  td.loss {{ color: #e05a5a; font-weight: 600; }}
  td.achko {{
    color: #f0b429;
    font-weight: 700;
    font-size: 13px;
  }}

  /* Detail panel */
  .detail {{
    display: none;
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: #1a2540;
    border-top: 2px solid #f0b429;
    border-radius: 18px 18px 0 0;
    padding: 20px 20px 30px;
    z-index: 100;
    animation: slideUp 0.25s ease;
  }}
  .detail.show {{ display: block; }}
  @keyframes slideUp {{
    from {{ transform: translateY(100%); opacity: 0; }}
    to   {{ transform: translateY(0);   opacity: 1; }}
  }}
  .detail-close {{
    position: absolute;
    top: 12px; right: 16px;
    background: none; border: none;
    color: #7a8aaa; font-size: 22px;
    cursor: pointer; line-height: 1;
  }}
  .detail h2 {{
    font-size: 20px;
    color: #f0b429;
    margin-bottom: 4px;
  }}
  .detail .achko-big {{
    font-size: 32px;
    font-weight: 800;
    color: #fff;
    line-height: 1;
    margin-bottom: 14px;
  }}
  .detail-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }}
  .stat-box {{
    background: #0e1117;
    border-radius: 10px;
    padding: 10px 8px;
    text-align: center;
  }}
  .stat-box .val {{
    font-size: 22px;
    font-weight: 700;
    color: #e8eef8;
  }}
  .stat-box .lbl {{
    font-size: 11px;
    color: #7a8aaa;
    margin-top: 2px;
  }}
  .stat-box.green .val {{ color: #4caf7d; }}
  .stat-box.red   .val {{ color: #e05a5a; }}
  .stat-box.gold  .val {{ color: #f0b429; }}
  .overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    z-index: 99;
  }}
  .overlay.show {{ display: block; }}
</style>
</head>
<body>
<div class="header">
  <h1>⚽ eFootball Reyting</h1>
  <p>O'yinchiga bosib batafsil ko'ring</p>
</div>

<div class="table-wrap">
<table>
  <thead>
    <tr>
      <th>№</th>
      <th style="text-align:left;padding-left:8px">O'yinchi</th>
      <th>O'</th>
      <th>G'</th>
      <th>D</th>
      <th>M</th>
      <th>Gol</th>
      <th>Achko</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</div>

<div class="overlay" id="overlay" onclick="closeDetail()"></div>
<div class="detail" id="detail">
  <button class="detail-close" onclick="closeDetail()">✕</button>
  <h2 id="d-name"></h2>
  <div class="achko-big" id="d-achko"></div>
  <div class="detail-grid">
    <div class="stat-box"><div class="val" id="d-o"></div><div class="lbl">O'yin</div></div>
    <div class="stat-box green"><div class="val" id="d-g"></div><div class="lbl">G'alaba</div></div>
    <div class="stat-box"><div class="val" id="d-d"></div><div class="lbl">Durang</div></div>
    <div class="stat-box red"><div class="val" id="d-m"></div><div class="lbl">Mag'lubiyat</div></div>
    <div class="stat-box gold"><div class="val" id="d-gol"></div><div class="lbl">Gollar</div></div>
    <div class="stat-box"><div class="val" id="d-streak"></div><div class="lbl">Streak</div></div>
  </div>
</div>

<script>
  try {{ Telegram.WebApp.ready(); Telegram.WebApp.expand(); }} catch(e) {{}}

  function showDetail(row) {{
    document.querySelectorAll("tbody tr").forEach(r => r.classList.remove("active"));
    row.classList.add("active");
    document.getElementById("d-name").textContent   = row.dataset.name;
    document.getElementById("d-achko").textContent  = "⭐ " + row.dataset.achko;
    document.getElementById("d-o").textContent      = row.dataset.o;
    document.getElementById("d-g").textContent      = row.dataset.g;
    document.getElementById("d-d").textContent      = row.dataset.d;
    document.getElementById("d-m").textContent      = row.dataset.m;
    document.getElementById("d-gol").textContent    = row.dataset.gol;
    document.getElementById("d-streak").textContent = row.dataset.streak;
    document.getElementById("detail").classList.add("show");
    document.getElementById("overlay").classList.add("show");
  }}

  function closeDetail() {{
    document.getElementById("detail").classList.remove("show");
    document.getElementById("overlay").classList.remove("show");
    document.querySelectorAll("tbody tr").forEach(r => r.classList.remove("active"));
  }}
</script>
</body>
</html>"""
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok", 200

def setup_webhook():
    webhook_url = f"{BASE_URL}/{TOKEN}"
    try:
        bot.delete_webhook(drop_pending_updates=True)
        bot.set_webhook(url=webhook_url)
        set_bot_commands(bot)
        logger.info(f"✅ Webhook o'rnatildi: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook o'rnatishda xatolik: {e}")

if __name__ == "__main__":
    logger.info("🚀 Bot ishga tushmoqda...")
    setup_webhook()
    app.run(host="0.0.0.0", port=PORT)
