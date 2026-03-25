import os
import re
import math
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
# GOOGLE SHEETS ULANISH
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


def ensure_headers():
    ranking_headers = [
        "Ism",
        "Oyinlar",
        "Galaba",
        "Durang",
        "Maglubiyat",
        "UrganGoli",
        "OtkazganGoli",
        "Achko",
        "Streak",
        "OxirgiNatija",
        "UpdatedAt",
    ]
    pending_headers = [
        "ID",
        "Player1",
        "Score1",
        "Score2",
        "Player2",
        "SubmittedByID",
        "SubmittedByName",
        "ChatID",
        "ChatTitle",
        "Status",
        "CreatedAt",
        "ApprovalMessageID",
    ]
    history_headers = [
        "ID",
        "Player1",
        "Score1",
        "Score2",
        "Player2",
        "SubmittedByName",
        "ApprovedByID",
        "ApprovedAt",
        "Delta1",
        "Delta2",
        "OldRating1",
        "NewRating1",
        "OldRating2",
        "NewRating2",
    ]

    if not ranking_ws.get_all_values():
        ranking_ws.append_row(ranking_headers)
    if not pending_ws.get_all_values():
        pending_ws.append_row(pending_headers)
    if not history_ws.get_all_values():
        history_ws.append_row(history_headers)


ensure_headers()


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip())
    if not name:
        return name
    return " ".join(word[:1].upper() + word[1:].lower() for word in name.split())


def esc(text: str) -> str:
    return html.escape(str(text))


def get_reply_menu():
    return ReplyKeyboardMarkup(
        [
            ["📊 Jadval", "🥇 Top 3"],
            ["📋 Menyu", "ℹ️ Qoida"],
        ],
        resize_keyboard=True,
    )


def parse_score_message(text: str):
    """
    Qabul qilinadigan formatlar:
    Nodir 3-2 Shaxzod
    nodir 3 : 2 shaxzod
    Ali 10 - 9 Vali
    """
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


def ranking_records():
    return ranking_ws.get_all_records()


def pending_records():
    return pending_ws.get_all_records()


def history_records():
    return history_ws.get_all_records()


def find_ranking_row(name: str):
    records = ranking_records()
    for idx, row in enumerate(records, start=2):
        if str(row["Ism"]).strip().lower() == name.strip().lower():
            return idx, row
    return None, None


def create_player_if_missing(name: str):
    row_idx, row = find_ranking_row(name)
    if row_idx:
        return row_idx, row

    ranking_ws.append_row(
        [
            name,
            0,   # Oyinlar
            0,   # Galaba
            0,   # Durang
            0,   # Maglubiyat
            0,   # UrganGoli
            0,   # OtkazganGoli
            INITIAL_RATING,  # Achko
            0,   # Streak
            "-", # OxirgiNatija
            now_str(),
        ]
    )
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

    # G'alabaga kichik bonus
    if s1 == 1.0:
        delta1 += bonus
        delta2 -= bonus
    elif s2 == 1.0:
        delta2 += bonus
        delta1 -= bonus

    # Yumshoq minimal o'zgarish
    if s1 == 1.0 and delta1 < 4:
        delta1 = 4
        delta2 = -4
    elif s2 == 1.0 and delta2 < 4:
        delta2 = 4
        delta1 = -4
    elif s1 == 0.5:
        if r1 < r2 and delta1 < 2:
            delta1 = 2
            delta2 = -2
        elif r2 < r1 and delta2 < 2:
            delta2 = 2
            delta1 = -2

    return round(delta1, 2), round(delta2, 2)


def update_player_stats(name: str, goals_for: int, goals_against: int, result: str, delta_rating: float):
    row_idx, row = create_player_if_missing(name)

    games = int(row["Oyinlar"]) + 1
    wins = int(row["Galaba"])
    draws = int(row["Durang"])
    losses = int(row["Maglubiyat"])
    gf = int(row["UrganGoli"]) + goals_for
    ga = int(row["OtkazganGoli"]) + goals_against
    rating = float(row["Achko"]) + float(delta_rating)
    streak = int(row["Streak"])

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
            name,
            games,
            wins,
            draws,
            losses,
            gf,
            ga,
            round(rating, 2),
            streak,
            last_result,
            now_str(),
        ]]
    )


def get_sorted_ranking():
    rows = ranking_records()
    rows = sorted(
        rows,
        key=lambda x: (
            float(x["Achko"]),
            int(x["Galaba"]),
            int(x["UrganGoli"]) - int(x["OtkazganGoli"]),
            int(x["UrganGoli"]),
        ),
        reverse=True
    )
    return rows


def format_top_banner(rows):
    if not rows:
        return "👑 Hali chempion yo‘q"

    top = rows[0]
    return (
        "🏆 <b>FIFA 07 REYTING BOT</b>\n\n"
        f"👑 <b>Chempion:</b> {esc(top['Ism'])}\n"
        f"⭐ <b>Achko:</b> {float(top['Achko']):.2f}\n"
        f"🎮 <b>O‘yin:</b> {top['Oyinlar']} | ✅ {top['Galaba']} | 🤝 {top['Durang']} | ❌ {top['Maglubiyat']}\n"
        f"⚽ <b>Gollar:</b> {top['UrganGoli']}-{top['OtkazganGoli']}"
    )


def format_table():
    rows = get_sorted_ranking()
    if not rows:
        return "Hali reytingda o‘yinchi yo‘q."

    lines = []
    lines.append("🏆 <b>FIFA 07 REYTING JADVALI</b>")
    lines.append("")

    # Chempion banner
    top = rows[0]
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"👑 <b>1. {esc(top['Ism'])}</b>")
    lines.append(
        f"🎮 O‘yin: {top['Oyinlar']} | ✅ {top['Galaba']} | 🤝 {top['Durang']} | ❌ {top['Maglubiyat']}"
    )
    lines.append(
        f"⚽ Gollar: {top['UrganGoli']}-{top['OtkazganGoli']} | ⭐ Achko: {float(top['Achko']):.2f}"
    )
    lines.append("━━━━━━━━━━━━━━")
    lines.append("")

    if len(rows) >= 2:
        second = rows[1]
        lines.append(f"🥈 <b>2. {esc(second['Ism'])}</b>")
        lines.append(
            f"🎮 O‘yin: {second['Oyinlar']} | ✅ {second['Galaba']} | 🤝 {second['Durang']} | ❌ {second['Maglubiyat']}"
        )
        lines.append(
            f"⚽ Gollar: {second['UrganGoli']}-{second['OtkazganGoli']} | ⭐ Achko: {float(second['Achko']):.2f}"
        )
        lines.append("")

    if len(rows) >= 3:
        third = rows[2]
        lines.append(f"🥉 <b>3. {esc(third['Ism'])}</b>")
        lines.append(
            f"🎮 O‘yin: {third['Oyinlar']} | ✅ {third['Galaba']} | 🤝 {third['Durang']} | ❌ {third['Maglubiyat']}"
        )
        lines.append(
            f"⚽ Gollar: {third['UrganGoli']}-{third['OtkazganGoli']} | ⭐ Achko: {float(third['Achko']):.2f}"
        )
        lines.append("")

    if len(rows) > 3:
        lines.append("📋 <b>Qolganlar:</b>")
        for i, row in enumerate(rows[3:], start=4):
            lines.append(
                f"{i}. <b>{esc(row['Ism'])}</b> — 🎮{row['Oyinlar']} | ✅{row['Galaba']} | "
                f"🤝{row['Durang']} | ❌{row['Maglubiyat']} | "
                f"⚽{row['UrganGoli']}-{row['OtkazganGoli']} | ⭐{float(row['Achko']):.2f}"
            )

    return "\n".join(lines)


def format_top3():
    rows = get_sorted_ranking()
    if not rows:
        return "Hali reyting yo‘q."

    lines = ["🥇 <b>TOP 3</b>", ""]
    medals = ["👑", "🥈", "🥉"]

    for i, row in enumerate(rows[:3], start=1):
        lines.append(
            f"{medals[i-1]} <b>{i}. {esc(row['Ism'])}</b> — ⭐ {float(row['Achko']):.2f} | "
            f"🎮 {row['Oyinlar']} | ✅ {row['Galaba']} | ⚽ {row['UrganGoli']}-{row['OtkazganGoli']}"
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
        "/pending - Direktor uchun kutilayotgan natijalar\n"
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


def is_director(user_id: int) -> bool:
    return user_id == DIRECTOR_ID


def add_pending_result(p1, s1, s2, p2, submitted_by_id, submitted_by_name, chat_id, chat_title):
    pending_id = str(uuid.uuid4())[:8]
    pending_ws.append_row([
        pending_id,
        p1,
        s1,
        s2,
        p2,
        submitted_by_id,
        submitted_by_name,
        chat_id,
        chat_title,
        "PENDING",
        now_str(),
        "",
    ])
    return pending_id


def find_pending_row(pending_id: str):
    rows = pending_records()
    for idx, row in enumerate(rows, start=2):
        if str(row["ID"]).strip() == pending_id:
            return idx, row
    return None, None


def set_pending_status(pending_id: str, status: str, message_id=None):
    row_idx, row = find_pending_row(pending_id)
    if not row_idx:
        return False

    approval_message_id = row.get("ApprovalMessageID", "")
    if message_id is not None:
        approval_message_id = str(message_id)

    pending_ws.update(
        f"A{row_idx}:L{row_idx}",
        [[
            row["ID"],
            row["Player1"],
            row["Score1"],
            row["Score2"],
            row["Player2"],
            row["SubmittedByID"],
            row["SubmittedByName"],
            row["ChatID"],
            row["ChatTitle"],
            status,
            row["CreatedAt"],
            approval_message_id,
        ]]
    )
    return True


def apply_approved_result(pending_row, approver_id):
    p1 = normalize_name(str(pending_row["Player1"]))
    p2 = normalize_name(str(pending_row["Player2"]))
    s1 = int(pending_row["Score1"])
    s2 = int(pending_row["Score2"])

    _, row1 = create_player_if_missing(p1)
    _, row2 = create_player_if_missing(p2)

    old1 = float(row1["Achko"])
    old2 = float(row2["Achko"])

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
        pending_row["ID"],
        p1,
        s1,
        s2,
        p2,
        pending_row["SubmittedByName"],
        approver_id,
        now_str(),
        delta1,
        delta2,
        old1,
        round(old1 + delta1, 2),
        old2,
        round(old2 + delta2, 2),
    ])

    set_pending_status(pending_row["ID"], "APPROVED")
    return delta1, delta2


# =========================
# BOT HANDLERLAR
# =========================
def set_bot_commands(bot):
    commands = [
        BotCommand("start", "Boshlash"),
        BotCommand("menu", "Menyu"),
        BotCommand("table", "Reyting jadvali"),
        BotCommand("top3", "Top 3"),
        BotCommand("pending", "Kutilayotgan natijalar"),
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

    update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_reply_menu(),
    )


def menu_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        format_menu_text(),
        parse_mode="HTML",
        reply_markup=get_reply_menu(),
    )


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        format_help_text(),
        parse_mode="HTML",
        reply_markup=get_reply_menu(),
    )


def table_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        format_table(),
        parse_mode="HTML",
        reply_markup=get_reply_menu(),
    )


def top3_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        format_top3(),
        parse_mode="HTML",
        reply_markup=get_reply_menu(),
    )


def pending_cmd(update: Update, context: CallbackContext):
    user = update.effective_user
    if not is_director(user.id):
        update.message.reply_text("⛔ Bu bo‘lim faqat Direktor uchun.")
        return

    rows = [r for r in pending_records() if str(r["Status"]).upper() == "PENDING"]
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


def handle_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    user = query.from_user
    query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, pending_id = data.split(":", 1)

    if not is_director(user.id):
        query.answer("Faqat Direktor tasdiqlay oladi.", show_alert=True)
        return

    row_idx, row = find_pending_row(pending_id)
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

        text = (
            f"✅ <b>Direktor tasdiqladi</b>\n\n"
            f"{p1} {s1}-{s2} {p2}\n\n"
            f"⭐ {p1}: {delta1:+.2f}\n"
            f"⭐ {p2}: {delta2:+.2f}\n\n"
            f"{format_top3()}"
        )
        query.edit_message_text(text, parse_mode="HTML")
    elif action == "reject":
        set_pending_status(pending_id, "REJECTED")
        text = (
            f"❌ <b>Direktor rad etdi</b>\n\n"
            f"{p1} {s1}-{s2} {p2}"
        )
        query.edit_message_text(text, parse_mode="HTML")


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
        p1=p1,
        s1=s1,
        s2=s2,
        p2=p2,
        submitted_by_id=submitted_by.id,
        submitted_by_name=submitted_by.full_name,
        chat_id=chat.id,
        chat_title=getattr(chat, "title", "") or "Private",
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve:{pending_id}"),
            InlineKeyboardButton("❌ Bekor qilish", callback_data=f"reject:{pending_id}"),
        ]
    ])

    msg = update.message.reply_text(
        "⏳ <b>Natija qabul qilindi</b>\n\n"
        f"🆔 <b>{esc(pending_id)}</b>\n"
        f"{esc(p1)} {s1}-{s2} {esc(p2)}\n\n"
        "Direktor tasdiqlashi kutilmoqda.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    set_pending_status(pending_id, "PENDING", message_id=msg.message_id)


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

    dp.add_handler(CallbackQueryHandler(handle_buttons))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_menu_buttons_text))

    print("Bot ishga tushdi...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
