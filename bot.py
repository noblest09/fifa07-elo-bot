import os
import re
import uuid
import html
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
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


# =========================
# RENDER UCHUN HEALTH SERVER
# =========================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"FIFA 07 bot is running")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server running on port {port}")
    server.serve_forever()


# =========================
# GOOGLE SHEETS
# =========================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
spreadsheet = client.open_by_key(SHEET_ID)


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
    except:
        return default


def safe_float(v, default=0.0):
    try:
        s = str(v).strip().replace(",", ".")
        if s == "":
            return default
        return float(s)
    except:
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

    return p1, s1, s2, p2


def sheet_rows(ws, headers):
    values = ws.get_all_values()
    result = []
    for idx, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(headers) - len(row))
        item = dict(zip(headers, row[:len(headers)]))
        if any(str(v).strip() for v in item.values()):
            result.append((idx, item))
    return result


def ranking_records():
    return sheet_rows(ranking_ws, RANKING_HEADERS)


def pending_records():
    return sheet_rows(pending_ws, PENDING_HEADERS)


def history_records():
    return sheet_rows(history_ws, HISTORY_HEADERS)


def find_ranking_row(name: str):
    for idx, row in ranking_records():
        if str(row["Ism"]).strip().lower() == name.strip().lower():
            return idx, row
    return None, None


def create_player_if_missing(name: str):
    row_idx, row = find_ranking_row(name)
    if row_idx:
        return row_idx, row

    ranking_ws.append_row([
        name, 0, 0, 0, 0, 0, 0, INITIAL_RATING, 0, "-", now_str()
    ])
    return find_ranking_row(name)


def expected_score(r1: float, r2: float) -> float:
    return 1 / (1 + 10 ** ((r2 - r1) / 400))


def calc_elo_change(r1: float, r2: float, score1: int, score2: int):
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

    delta1 = K_FACTOR * (s1 - e1)
    delta2 = K_FACTOR * (s2 - e2)

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

    ranking_ws.update(
        f"A{row_idx}:K{row_idx}",
        [[
            name, games, wins, draws, losses,
            gf, ga, round(rating, 2), streak, last_result, now_str()
        ]]
    )


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
            "🏆 <b>FIFA 07 REYTING BOT</b>\n\n"
            "👑 <b>Chempion:</b> Hali yo‘q\n"
            "⭐ <b>Achko:</b> -"
        )

    top = rows[0]
    return (
        "🏆 <b>FIFA 07 REYTING BOT</b>\n\n"
        f"👑 <b>Chempion:</b> {esc(top['Ism'])}\n"
        f"⭐ <b>Achko:</b> {safe_float(top['Achko']):.2f}\n"
        f"🎮 <b>O‘yin:</b> {top['Oyinlar']} | ✅ {top['Galaba']} | 🤝 {top['Durang']} | ❌ {top['Maglubiyat']}\n"
        f"⚽ <b>Gollar:</b> {top['UrganGoli']}-{top['OtkazganGoli']}"
    )


def format_top3():
    rows = get_sorted_ranking()
    if not rows:
        return "🏅 TOP 3\n\nHali reyting yo‘q."

    lines = ["🏅 <b>TOP 3</b>", ""]

    medals = ["👑", "🥈", "🥉"]
    faces = ["😎", "🎮", "⚽"]

    for i, row in enumerate(rows[:3], start=1):
        medal = medals[i - 1]
        face = faces[i - 1] if i - 1 < len(faces) else "🎯"
        lines.append(
            f"{medal} <b>{i}. {esc(row['Ism'])}</b> — ⭐ {safe_float(row['Achko']):.2f} | "
            f"{face} {row['Oyinlar']} | ✅ {row['Galaba']} | ⚽ {row['UrganGoli']}-{row['OtkazganGoli']}"
        )

    return "\n".join(lines)


def format_table():
    rows = get_sorted_ranking()
    if not rows:
        return "🏆 <b>FIFA 07 REYTING JADVALI</b>\n\nHali reytingda o‘yinchi yo‘q."

    lines = ["🏆 <b>FIFA 07 REYTING JADVALI</b>", ""]

    top = rows[0]
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"👑 <b>1. {esc(top['Ism'])}</b>")
    lines.append(
        f"⚽ O‘yin: {top['Oyinlar']} | ✅ {top['Galaba']} | 🤝 {top['Durang']} | ❌ {top['Maglubiyat']}"
    )
    lines.append(
        f"🥅 Gollar: {top['UrganGoli']}-{top['OtkazganGoli']} | ⭐ Achko: {safe_float(top['Achko']):.2f}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if len(rows) >= 2:
        second = rows[1]
        lines.append("")
        lines.append(f"🥈 <b>2. {esc(second['Ism'])}</b>")
        lines.append(
            f"⚽ O‘yin: {second['Oyinlar']} | ✅ {second['Galaba']} | 🤝 {second['Durang']} | ❌ {second['Maglubiyat']}"
        )
        lines.append(
            f"🥅 Gollar: {second['UrganGoli']}-{second['OtkazganGoli']} | ⭐ Achko: {safe_float(second['Achko']):.2f}"
        )

    if len(rows) >= 3:
        third = rows[2]
        lines.append("")
        lines.append(f"🥉 <b>3. {esc(third['Ism'])}</b>")
        lines.append(
            f"⚽ O‘yin: {third['Oyinlar']} | ✅ {third['Galaba']} | 🤝 {third['Durang']} | ❌ {third['Maglubiyat']}"
        )
        lines.append(
            f"🥅 Gollar: {third['UrganGoli']}-{third['OtkazganGoli']} | ⭐ Achko: {safe_float(third['Achko']):.2f}"
        )

    if len(rows) > 3:
        lines.append("")
        lines.append("📋 <b>Qolganlar:</b>")
        for i, row in enumerate(rows[3:], start=4):
            lines.append(
                f"{i}. <b>{esc(row['Ism'])}</b> — ⭐ {safe_float(row['Achko']):.2f} | "
                f"🎮 {row['Oyinlar']} | ✅ {row['Galaba']} | 🤝 {row['Durang']} | ❌ {row['Maglubiyat']} | "
                f"⚽ {row['UrganGoli']}-{row['OtkazganGoli']}"
            )

    return "\n".join(lines)


def format_menu_text():
    return (
        "📋 <b>Bot menyusi</b>\n\n"
        "Natija yuborish:\n"
        "<code>Nodir 3-2 Shaxzod</code>\n\n"
        "Komandalar:\n"
        "/start - Boshlash\n"
        "/menu - Menyu\n"
        "/table - To‘liq jadval\n"
        "/top3 - Top 3\n"
        "/pending - Kutilayotgan natijalar\n"
        "/reset - Reytingni tozalash (Direktor)\n"
        "/restart - Botni qayta ishga tushirish (Direktor)\n"
        "/help - Qoidalar"
    )


def format_help_text():
    return (
        "ℹ️ <b>Qoidalar</b>\n\n"
        "1) Guruhdagi istalgan odam natija yuborishi mumkin.\n"
        "2) Natija darrov hisoblanmaydi.\n"
        "3) Tasdiqlash faqat <b>Direktor</b> tomonidan bo‘ladi.\n"
        "4) Achko ELOga o‘xshash hisoblanadi:\n"
        "   - kuchli kuchsizni yutsa kamroq oladi\n"
        "   - kuchsiz kuchlini yutsa ko‘proq oladi\n"
        "5) To‘g‘ri format:\n"
        "<code>Ali 4-3 Vali</code>"
    )


def add_pending_result(p1, s1, s2, p2, submitted_by_id, submitted_by_name, chat_id, chat_title):
    pending_id = str(uuid.uuid4())[:8]
    pending_ws.append_row([
        pending_id, p1, s1, s2, p2,
        submitted_by_id, submitted_by_name, chat_id, chat_title,
        "PENDING", now_str(), ""
    ])
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

    pending_ws.update(
        f"A{row_idx}:L{row_idx}",
        [[
            row["ID"], row["Player1"], row["Score1"], row["Score2"], row["Player2"],
            row["SubmittedByID"], row["SubmittedByName"], row["ChatID"], row["ChatTitle"],
            status, row["CreatedAt"], approval_message_id
        ]]
    )
    return True


def apply_approved_result(pending_row, approver_id):
    p1 = normalize_name(str(pending_row["Player1"]))
    p2 = normalize_name(str(pending_row["Player2"]))
    s1 = safe_int(pending_row["Score1"])
    s2 = safe_int(pending_row["Score2"])

    _, row1 = create_player_if_missing(p1)
    _, row2 = create_player_if_missing(p2)

    old1 = safe_float(row1["Achko"], INITIAL_RATING)
    old2 = safe_float(row2["Achko"], INITIAL_RATING)

    delta1, delta2 = calc_elo_change(old1, old2, s1, s2)

    if s1 > s2:
        res1, res2 = "W", "L"
    elif s1 < s2:
        res1, res2 = "L", "W"
    else:
        res1 = res2 = "D"

    update_player_stats(p1, s1, s2, res1, delta1)
    update_player_stats(p2, s2, s1, res2, delta2)

    history_ws.append_row([
        pending_row["ID"], p1, s1, s2, p2,
        pending_row["SubmittedByName"], approver_id, now_str(),
        delta1, delta2, old1, round(old1 + delta1, 2), old2, round(old2 + delta2, 2)
    ])

    set_pending_status(pending_row["ID"], "APPROVED")
    return delta1, delta2


# =========================
# KOMANDALAR
# =========================
def set_bot_commands(bot):
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
    bot.set_my_commands(commands)


def start(update: Update, context: CallbackContext):
    rows = get_sorted_ranking()
    text = format_top_banner(rows) + "\n\n" + (
        "👋 <b>FIFA 07 Reyting botiga xush kelibsiz!</b>\n\n"
        "Natija yuborish formati:\n"
        "<code>Nodir 3-2 Shaxzod</code>"
    )
    update.message.reply_text(text, parse_mode="HTML", reply_markup=get_reply_menu())


def menu_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_menu_text(), parse_mode="HTML", reply_markup=get_reply_menu())


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_help_text(), parse_mode="HTML", reply_markup=get_reply_menu())


def table_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_table(), parse_mode="HTML", reply_markup=get_reply_menu())


def top3_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(format_top3(), parse_mode="HTML", reply_markup=get_reply_menu())


def pending_cmd(update: Update, context: CallbackContext):
    if not is_director(update.effective_user.id):
        update.message.reply_text("⛔ Bu bo‘lim faqat Direktor uchun.")
        return

    rows = [row for _, row in pending_records() if str(row["Status"]).upper() == "PENDING"]
    if not rows:
        update.message.reply_text("✅ Kutilayotgan natija yo‘q.")
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
        update.message.reply_text("⛔ /reset faqat Direktor uchun.")
        return

    ranking_ws.clear()
    pending_ws.clear()
    history_ws.clear()

    ensure_headers(ranking_ws, RANKING_HEADERS)
    ensure_headers(pending_ws, PENDING_HEADERS)
    ensure_headers(history_ws, HISTORY_HEADERS)

    update.message.reply_text("✅ Reyting, pending va history tozalandi.")


def restart_cmd(update: Update, context: CallbackContext):
    if not is_director(update.effective_user.id):
        update.message.reply_text("⛔ /restart faqat Direktor uchun.")
        return

    update.message.reply_text("🔄 Bot qayta ishga tushirilmoqda...")
    os.execl(os.sys.executable, os.sys.executable, *os.sys.argv)


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
            query.answer("Faqat Direktor tasdiqlay oladi.", show_alert=True)
            return

        _, row = find_pending_row(pending_id)
        if not row:
            query.edit_message_text("❌ Bu pending natija topilmadi.")
            return

        status = str(row["Status"]).upper()
        if status != "PENDING":
            query.answer("Bu natija allaqachon ko‘rib chiqilgan.", show_alert=True)
            return

        p1 = esc(row["Player1"])
        p2 = esc(row["Player2"])
        s1 = row["Score1"]
        s2 = row["Score2"]

        if action == "approve":
            delta1, delta2 = apply_approved_result(row, user.id)
            query.edit_message_text(
                f"✅ <b>Direktor tasdiqladi</b>\n\n"
                f"{p1} {s1}-{s2} {p2}\n\n"
                f"⭐ {p1}: {delta1:+.2f}\n"
                f"⭐ {p2}: {delta2:+.2f}",
                parse_mode="HTML",
            )
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text=format_top3(),
                parse_mode="HTML"
            )

        elif action == "reject":
            set_pending_status(pending_id, "REJECTED")
            query.edit_message_text(
                f"❌ <b>Direktor rad etdi</b>\n\n{p1} {s1}-{s2} {p2}",
                parse_mode="HTML",
            )

    except Exception as e:
        print("BUTTON ERROR:", e)
        context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Tasdiqlashda xatolik:\n<code>{esc(str(e))}</code>",
            parse_mode="HTML",
        )


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
        "Direktor tasdiqlashi kutilmoqda.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# =========================
# MAIN
# =========================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    token = os.environ["TELEGRAM_TOKEN"]
    updater = Updater(token, use_context=True)
    dp = updater.dispatcher

    set_bot_commands(updater.bot)

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("menu", menu_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("table", table_cmd))
    dp.add_handler(CommandHandler("top3", top3_cmd))
    dp.add_handler(CommandHandler("pending", pending_cmd))
    dp.add_handler(CommandHandler("reset", reset_cmd))
    dp.add_handler(CommandHandler("restart", restart_cmd))

    dp.add_handler(CallbackQueryHandler(handle_buttons))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_menu_buttons_text))

    print("Bot ishga tushdi...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
