import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from openai import OpenAI
import asyncio

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Tracks nudge tasks, last action, and resistance level per user
nudge_tasks = {}
user_last_action = {}
user_resistance_level = {}

RESISTANCE_WORDS = [
    "males", "ga mau", "nanti aja", "nanti", "capek",
    "later", "ga bisa", "susah", "malas", "belum",
    "ga sanggup", "overwhelmed", "berat"
]

def detect_intent(message):
    message = message.lower()
    if any(word in message for word in ["stuck", "confused", "ga tau", "bingung", "mulai dari mana"]):
        return "STUCK"
    elif any(word in message for word in ["overwhelmed", "too many", "banyak banget", "overwhelm", "banyak tugas"]):
        return "OVERWHELMED"
    elif any(word in message for word in ["tired", "no energy", "capek", "lelah", "males", "exhausted"]):
        return "LOW_ENERGY"
    else:
        return "STUCK"

def is_resistance(message):
    message = message.lower()
    return any(word in message for word in RESISTANCE_WORDS)

def is_done(message):
    message = message.lower()
    return any(word in message for word in ["done", "sudah", "udah", "selesai", "beres", "oke", "ok", "yes", "ya"])

def ask_narai(user_message, intent):
    system_prompt = """Kamu adalah narAI, teman yang selalu ada buat bantuin user mulai ngerjain sesuatu.

Gaya ngobrol kamu:
- Casual, hangat, kayak teman deket
- Pakai bahasa sehari-hari (lo/gue kalau user pakai bahasa Indonesia)
- Pendek dan to the point, ga bertele-tele
- Ga perlu formal sama sekali
- Kalau user nulis bahasa Inggris, balas bahasa Inggris yang casual juga

Yang harus kamu lakuin:
- Kasih SATU langkah kecil yang bisa langsung dikerjain sekarang
- Maksimal 2 kalimat
- Bisa dikerjain dalam 10 menit atau kurang
- Akhiri dengan dorongan kecil yang natural, kayak "yuk mulai sekarang" atau "coba dulu deh"
- Kalau LOW_ENERGY: buat tugasnya super kecil, 2-3 menit aja
- Kalau OVERWHELMED: pilih SATU tugas dari yang dia sebut, jangan tanya balik
- Kalau STUCK: kasih langkah pertama yang paling gampang"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Intent: {intent}\nMessage: {user_message}"}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_simplified(last_action, resistance_level):
    if resistance_level == 1:
        prompt = f"""User menolak untuk melakukan ini: {last_action}

Buat versi yang LEBIH KECIL dari tugas itu. 
Contoh: kalau tugasnya "tulis 3 poin", jadi "tulis 1 poin aja".
Tetap casual, hangat, 1-2 kalimat, pakai lo/gue."""

    else:
        prompt = f"""User masih menolak. Tugas sebelumnya: {last_action}

Buat versi yang PALING KECIL mungkin — sekecil "buka aplikasinya aja" atau "ambil laptopnya dulu".
Tetap casual, hangat, 1-2 kalimat, pakai lo/gue."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Kamu adalah narAI, teman casual yang bantu user mulai kerja. Selalu pakai bahasa lo/gue yang santai."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

async def send_nudge(context, chat_id, nudge_number):
    if nudge_number == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Eh gimana, udah mulai belum? 👀"
        )
    elif nudge_number == 2:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Gapapa, coba 2 menit aja deh. Ga perlu selesai, yang penting mulai. 💪"
        )

async def nudge_sequence(context, chat_id):
    await asyncio.sleep(3600)
    await send_nudge(context, chat_id, 1)
    await asyncio.sleep(7200)
    await send_nudge(context, chat_id, 2)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text

    # Cancel existing nudge if user replies
    if chat_id in nudge_tasks:
        nudge_tasks[chat_id].cancel()
        del nudge_tasks[chat_id]

    # Check if user is done
    if is_done(user_message) and chat_id in user_last_action:
        user_resistance_level[chat_id] = 0
        await update.message.reply_text("Niceee, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?")
        return

    # Check if user is resisting
    if is_resistance(user_message) and chat_id in user_last_action:
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)

        simplified = ask_narai_simplified(user_last_action[chat_id], level)
        user_last_action[chat_id] = simplified
        await update.message.reply_text(simplified)

    else:
        # Normal flow
        user_resistance_level[chat_id] = 0
        intent = detect_intent(user_message)
        reply = ask_narai(user_message, intent)
        user_last_action[chat_id] = reply
        await update.message.reply_text(reply)
        await update.message.reply_text("Gue bakal check in sama lo sekitar 1 jam lagi ya. Gas! 🔥")

    # Restart nudge sequence
    task = asyncio.create_task(nudge_sequence(context, chat_id))
    nudge_tasks[chat_id] = task

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("narAI is running...")
app.run_polling()
