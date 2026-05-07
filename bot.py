import os
import base64
import fitz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI
import asyncio

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

nudge_tasks = {}
user_last_action = {}
user_resistance_level = {}
user_state = {}

# States
CLARIFYING = "CLARIFYING"
ACTION_SENT = "ACTION_SENT"

RESISTANCE_WORDS = [
    "males", "ga mau", "nanti aja", "nanti", "capek",
    "later", "ga bisa", "susah", "malas", "belum",
    "ga sanggup", "berat"
]

CLARIFYING_PHRASES = [
    "susun prioritas", "prioritas", "ga tau mau ngerjain apa",
    "bingung mau ngapain", "harus ngapain", "mulai dari mana",
    "help me prioritize", "what should i do", "ga tau", "confused"
]

def action_buttons():
    keyboard = [[
        InlineKeyboardButton("✅ Udah done!", callback_data="done"),
        InlineKeyboardButton("😩 Males ah", callback_data="resist")
    ]]
    return InlineKeyboardMarkup(keyboard)

def detect_intent(message):
    message = message.lower()
    if any(word in message for word in ["stuck", "confused", "ga tau", "bingung", "mulai dari mana"]):
        return "STUCK"
    elif any(word in message for word in ["overwhelmed", "too many", "banyak banget", "overwhelm", "banyak tugas"]):
        return "OVERWHELMED"
    elif any(word in message for word in ["tired", "no energy", "capek", "lelah", "malas", "exhausted"]):
        return "LOW_ENERGY"
    else:
        return "STUCK"

def is_clarifying(message):
    message = message.lower()
    return any(phrase in message for phrase in CLARIFYING_PHRASES)

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

def ask_narai_clarify(user_message):
    prompt = f"""User bilang: "{user_message}"

Mereka belum tau mau ngerjain apa. Tugas lo adalah bantu mereka identify SATU tugas konkret.

Caranya:
- Tanya satu pertanyaan singkat yang bantu mereka fokus
- Contoh: "Oke, dari semua yang ada di kepala lo sekarang, tugas apa yang paling bikin lo kepikiran?"
- Atau: "Kalau lo harus selesaiin satu hal hari ini, apa yang itu?"
- Maksimal 2 kalimat
- Casual, pakai lo/gue
- Jangan kasih action dulu, bantu mereka identify tugasnya"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Kamu adalah narAI, teman casual yang bantu user mulai kerja."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_from_list(content_text):
    prompt = f"""User ini ngirim list tugas mereka. Ini isinya:

{content_text}

Tugas lo:
- Pilih SATU tugas yang paling konkret atau paling mudah dimulai
- Kasih satu langkah pertama yang bisa langsung dikerjain sekarang
- Maksimal 2 kalimat
- Casual, pakai lo/gue
- Akhiri dengan dorongan kecil"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Kamu adalah narAI, teman casual yang bantu user mulai kerja."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=150
    )
    return response.choices[0].message.content

def ask_narai_from_image(image_base64):
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """Lo adalah narAI, teman casual yang bantu user mulai kerja.
User ngirim gambar berisi list tugas atau catatan mereka.

Tugas lo:
- Baca semua yang ada di gambar
- Pilih SATU tugas yang paling konkret atau paling mudah dimulai
- Kasih satu langkah pertama yang bisa langsung dikerjain sekarang
- Maksimal 2 kalimat
- Casual, pakai lo/gue
- Akhiri dengan dorongan kecil

Langsung kasih actionnya, jangan jelasin apa yang lo lihat."""
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }
        ],
        max_tokens=150
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

Buat versi yang PALING KECIL mungkin seperti "buka aplikasinya aja" atau "ambil laptopnya dulu".
Tetap casual, hangat, 1-2 kalimat, pakai lo/gue."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Kamu adalah narAI, teman casual yang bantu user mulai kerja."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

async def send_nudge(context, chat_id, nudge_number):
    if nudge_number == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Eh gimana, udah mulai belum? 👀",
            reply_markup=action_buttons()
        )
    elif nudge_number == 2:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Gapapa, coba 2 menit aja deh. Ga perlu selesai, yang penting mulai. 💪",
            reply_markup=action_buttons()
        )

async def nudge_sequence(context, chat_id):
    await asyncio.sleep(3600)
    await send_nudge(context, chat_id, 1)
    await asyncio.sleep(7200)
    await send_nudge(context, chat_id, 2)

def start_nudge(context, chat_id):
    if chat_id in nudge_tasks:
        nudge_tasks[chat_id].cancel()
    task = asyncio.create_task(nudge_sequence(context, chat_id))
    nudge_tasks[chat_id] = task

def stop_nudge(chat_id):
    if chat_id in nudge_tasks:
        nudge_tasks[chat_id].cancel()
        del nudge_tasks[chat_id]

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    stop_nudge(chat_id)

    if query.data == "done":
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Niceee, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?"
        )

    elif query.data == "resist":
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        await query.edit_message_reply_markup(reply_markup=None)

        simplified = ask_narai_simplified(user_last_action.get(chat_id, "tugas lo"), level)
        user_last_action[chat_id] = simplified
        user_state[chat_id] = ACTION_SENT

        await context.bot.send_message(
            chat_id=chat_id,
            text=simplified,
            reply_markup=action_buttons()
        )
        start_nudge(context, chat_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text
    current_state = user_state.get(chat_id, CLARIFYING)

    stop_nudge(chat_id)

    # User says done
    if is_done(user_message) and current_state == ACTION_SENT:
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        await update.message.reply_text("Niceee, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?")
        return

    # User is resisting
    if is_resistance(user_message) and current_state == ACTION_SENT:
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        simplified = ask_narai_simplified(user_last_action[chat_id], level)
        user_last_action[chat_id] = simplified
        user_state[chat_id] = ACTION_SENT
        await update.message.reply_text(simplified, reply_markup=action_buttons())
        start_nudge(context, chat_id)
        return

    # User is still figuring out what to do
    if is_clarifying(user_message):
        user_state[chat_id] = CLARIFYING
        reply = ask_narai_clarify(user_message)
        await update.message.reply_text(reply)
        # No buttons, no nudge yet
        return

    # User has a clear task — give action, start nudge
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT
    intent = detect_intent(user_message)
    reply = ask_narai(user_message, intent)
    user_last_action[chat_id] = reply
    await update.message.reply_text(reply, reply_markup=action_buttons())
    await update.message.reply_text("Gue bakal check in sama lo sekitar 1 jam lagi ya. Gas! 🔥")
    start_nudge(context, chat_id)

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)

    await update.message.reply_text("Bentar ya, gue liat dulu list lo... 👀")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_base64 = base64.b64encode(file_bytes).decode("utf-8")

    reply = ask_narai_from_image(image_base64)
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT

    await update.message.reply_text(reply, reply_markup=action_buttons())
    await update.message.reply_text("Gue bakal check in sama lo sekitar 1 jam lagi ya. Gas! 🔥")
    start_nudge(context, chat_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Sekarang gue cuma bisa baca PDF sama gambar ya. Coba kirim dalam format itu!")
        return

    await update.message.reply_text("Bentar ya, gue baca PDF lo dulu... 📄")

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    pdf = fitz.open(stream=bytes(file_bytes), filetype="pdf")
    text = ""
    for page in pdf:
        text += page.get_text()

    if not text.strip():
        await update.message.reply_text("Hmm, PDF-nya kayaknya kosong atau ga bisa dibaca. Coba kirim sebagai gambar aja!")
        return

    reply = ask_narai_from_list(text[:2000])
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT

    await update.message.reply_text(reply, reply_markup=action_buttons())
    await update.message.reply_text("Gue bakal check in sama lo sekitar 1 jam lagi ya. Gas! 🔥")
    start_nudge(context, chat_id)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
app.add_handler(CallbackQueryHandler(handle_button))

print("narAI is running...")
app.run_polling()
