from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Google Sheets bog‘lash
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key("108hVJMPQNTYfrdUV1VOFXgi_v144jev0DeZiaUm4How").sheet1
ADMIN_ID = 934386169  # <-- bu yerga o'z Telegram ID'ingizni yozing

import os
import sys

def restart(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id == ADMIN_ID:
        update.message.reply_text("🔄 Bot qayta ishga tushirilmoqda...")
        os.execl(sys.executable, sys.executable, *sys.argv)
    else:
        update.message.reply_text("🚫 Sizda /restart buyrug‘ini ishlatishga ruxsat yo‘q.")
def reset_table(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id == ADMIN_ID:
        sheet.resize(1)  # faqat 1-qator (sarlavhalar) qoldiriladi
        update.message.reply_text("❗ Reyting tozalandi. 0 dan boshlandi.")
    else:
        update.message.reply_text("🚫 Sizda /reset buyrug‘iga ruxsat yo‘q.")

# Reyting yangilash funksiyasi
def update_rating(p1, g1, p2, g2):
    records = sheet.get_all_records()
    names = [r["Ism"] for r in records]

    def add_or_update(name, is_win, is_draw, gf, ga):
        if name in names:
            i = names.index(name)
            row = i + 2  # headerdan keyin boshlanadi
            r = records[i]
            games = r["O'yinlar"] + 1
            wins = r["G'alaba"] + (1 if is_win else 0)
            draws = r["Durrang"] + (1 if is_draw else 0)
            losses = r["Mag'lubiyat"] + (0 if is_win or is_draw else 1)
            gf += r["Urgan goli"]
            ga += r["Otkazgan goli"]
            points = wins * 3 + draws
            sheet.update(
                range_name = f"B{row}:H{row}",
                values = [[games, wins, draws, losses, gf, ga, points]]
            )
        else:
            games = 1
            wins = 1 if is_win else 0
            draws = 1 if is_draw else 0
            losses = 0 if is_win or is_draw else 1
            points = wins * 3 + draws
            sheet.append_row([name, games, wins, draws, losses, gf, ga, points])

    g1 = int(g1)
    g2 = int(g2)
    if g1 == g2:
        add_or_update(p1, False, True, g1, g2)
        add_or_update(p2, False, True, g2, g1)
    elif g1 > g2:
        add_or_update(p1, True, False, g1, g2)
        add_or_update(p2, False, False, g2, g1)
    else:
        add_or_update(p1, False, False, g1, g2)
        add_or_update(p2, True, False, g2, g1)

# /start buyrug‘i
def start(update: Update, context: CallbackContext):
    update.message.reply_text("👋 FIFA 07 Reyting bot! Natijani shu formatda yuboring: NODIR 3-1 SHAXZOD")

# /table buyrug‘i
def show_table(update: Update, context: CallbackContext):
    all_records = sheet.get_all_records()
    sorted_records = sorted(all_records, key=lambda x: x['Ochko'], reverse=True)

    text = "🏆 <b>FIFA 07 Reyting</b>\n"
    for i, row in enumerate(sorted_records, 1):
        text += "{}. {:<10} | {} o‘yin | {} g‘alaba | {} ochko\n".format(
            i,
            row['Ism'],
            row["O'yinlar"],
            row["G'alaba"],
            row["Ochko"]
        )

    update.message.reply_text(text, parse_mode='HTML')

# Oddiy xabar (natijalar) uchun
def handle_message(update, context):
    msg = update.message.text.upper()
    if "-" in msg:
        try:
            left, right = msg.split("-")
            p1, g1 = left.strip().rsplit(" ", 1)
            g2, p2 = right.strip().split(" ", 1)
            update_rating(p1, g1, p2, g2)
            update.message.reply_text(f"✅ Reyting yangilandi: {p1} {g1}-{g2} {p2}")
            show_table(update, context)
        except Exception as e:
            update.message.reply_text("❗ Format noto‘g‘ri. Masalan: NODIR 2-1 SHAXZOD")
            print("Xatolik:", e)

# Botni ishga tushirish
from telegram import Update
from telegram.ext import CallbackContext

def start(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    print("Sizning Telegram ID:", user_id)  # Terminalga chiqariladi
    update.message.reply_text("👋 Salom! Reyting bot ishga tushdi.")

def main():
    TOKEN = "7288273012:AAGBEa9BoJocxiRhIrLcnxzRoag-2HZRy40"
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("restart", restart))
    dp.add_handler(CommandHandler("reset", reset_table))

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("table", show_table))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
