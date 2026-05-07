import os
import base64
import fitz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes
from openai import OpenAI
from supabase import create_client
import asyncio

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

nudge_tasks = {}
user_last_action = {}
user_resistance_level = {}
user_state = {}

ADMIN_ID = 1110057425

CLARIFYING = "CLARIFYING"
ACTION_SENT = "ACTION_SENT"
ONBOARDING_NAME = "ONBOARDING_NAME"
ONBOARDING_JOB = "ONBOARDING_JOB"
ONBOARDING_STRUGGLE = "ONBOARDING_STRUGGLE"

RESISTANCE_WORDS = [
    "males", "ga mau", "nanti aja", "nanti",
    "later", "ga bisa", "susah", "malas",
    "ga sanggup", "berat"
]

def action_buttons():
    keyboard = [[
        InlineKeyboardButton("✅ Udah done!", callback_data="done"),
        InlineKeyboardButton("😩 Males ah", callback_data="resist")
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_user(user_id):
    try:
        result = supabase.table("users").select("*").eq("user_id", user_id).execute()
        if result.data:
            return result.data[0]
        return None
    except:
        return None

def save_user(user_id, name, job, struggle):
    try:
        supabase.table("users").upsert({
            "user_id": user_id,
            "name": name,
            "job": job,
            "biggest_struggle": struggle
        }).execute()
    except:
        pass

def log_session(user_id, action, outcome):
    try:
        supabase.table("sessions").insert({
            "user_id": user_id,
            "action_sent": action,
            "outcome": outcome
        }).execute()
    except:
        pass

def get_stats():
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        all_users = supabase.table("users").select("*").execute()
        total_users = len(all_users.data)

        new_today = supabase.table("users").select("*").gte("created_at", today).execute()
        new_users_today = len(new_today.data)

        all_sessions = supabase.table("sessions").select("*").execute()
        total_actions = len([s for s in all_sessions.data if s["outcome"] == "sent"])
        total_done = len([s for s in all_sessions.data if s["outcome"] == "done"])
        total_resist = len([s for s in all_sessions.data if s["outcome"] == "resist"])

        completion_rate = 0
        if total_done + total_resist > 0:
            completion_rate = round(total_done / (total_done + total_resist) * 100)

        return {
            "total_users": total_users,
            "new_today": new_users_today,
            "total_actions": total_actions,
            "total_done": total_done,
            "total_resist": total_resist,
            "completion_rate": completion_rate
        }
    except:
        return None

def is_resistance(message):
    message = message.lower()
    return any(word in message for word in RESISTANCE_WORDS)

def is_done(message):
    message = message.lower()
    return any(word in message for word in ["done", "sudah", "udah", "selesai", "beres", "ok", "yes", "ya", "yep"])

def classify_message(user_message):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """Kamu adalah classifier. Jawab HANYA dengan satu kata:
- HAS_TASK → kalau user menyebut tugas spesifik
- NO_TASK → kalau user tidak menyebut tugas spesifik"""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=10
    )
    result = response.choices[0].message.content.strip().upper()
    return "HAS_TASK" if "HAS_TASK" in result else "NO_TASK"

def ask_narai_clarify(user_message, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"Nama user adalah {name}." if name else ""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""Kamu adalah narAI, teman casual yang bantu user mulai kerja. {name_str}
Tanya SATU pertanyaan singkat untuk bantu mereka identify satu tugas konkret.
Maksimal 2 kalimat. Casual, pakai lo/gue. Jangan kasih action dulu."""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_action(user_message, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    struggle = user_profile.get("biggest_struggle", "") if user_profile else ""

    context_str = ""
    if name:
        context_str += f"Nama user adalah {name}. "
    if struggle:
        context_str += f"Mereka biasanya paling struggle dengan: {struggle}. "

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""Kamu adalah narAI, teman yang selalu ada buat bantuin user mulai ngerjain sesuatu. {context_str}

Gaya ngobrol kamu:
- Casual, hangat, kayak teman deket
- Pakai nama mereka kalau kamu tau
- Pakai bahasa sehari-hari
- Pendek dan to the point

Yang harus kamu lakuin:
- Kasih SATU langkah kecil yang bisa langsung dikerjain sekarang
- Maksimal 2 kalimat
- Bisa dikerjain dalam 10 menit atau kurang
- Akhiri dengan dorongan kecil"""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_simplified(last_action, resistance_level, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"Nama user adalah {name}." if name else ""

    if resistance_level == 1:
        prompt = f"""{name_str} User menolak untuk melakukan ini: {last_action}
Buat versi yang LEBIH KECIL. Casual, 1-2 kalimat, pakai lo/gue."""
    else:
        prompt = f"""{name_str} User masih menolak. Tugas sebelumnya: {last_action}
Buat versi PALING KECIL mungkin seperti "buka aplikasinya aja". Casual, 1-2 kalimat, pakai lo/gue."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Kamu adalah narAI, teman casual yang bantu user mulai kerja."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_from_image(image_base64, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"Nama user adalah {name}." if name else ""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"""Lo adalah narAI, teman casual yang bantu user mulai kerja. {name_str}
User ngirim gambar berisi list tugas mereka.
- Baca semua yang ada di gambar
- Pilih SATU tugas paling konkret atau paling mudah
- Kasih satu langkah pertama
- Maksimal 2 kalimat, casual, pakai lo/gue
- Pakai nama mereka kalau kamu tau
- Akhiri dengan dorongan kecil"""
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

def ask_narai_from_list(content_text, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"Nama user adalah {name}." if name else ""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"Kamu adalah narAI, teman casual yang bantu user mulai kerja. {name_str}"
            },
            {
                "role": "user",
                "content": f"""User ngirim list tugas mereka:

{content_text}

Pilih SATU tugas paling konkret. Kasih satu langkah pertama. Maksimal 2 kalimat. Casual, pakai lo/gue. Pakai nama mereka kalau kamu tau. Akhiri dengan dorongan kecil."""
            }
        ],
        max_tokens=150
    )
    return response.choices[0].message.content

async def send_nudge(context, chat_id, nudge_number, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f" {name}" if name else ""

    if nudge_number == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Eh{name_str}, gimana? Udah mulai belum? 👀",
            reply_markup=action_buttons()
        )
    elif nudge_number == 2:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Gapapa{name_str}, coba 2 menit aja deh. Ga perlu selesai, yang penting mulai. 💪",
            reply_markup=action_buttons()
        )

async def nudge_sequence(context, chat_id, user_profile=None):
    await asyncio.sleep(3600)
    await send_nudge(context, chat_id, 1, user_profile)
    await asyncio.sleep(7200)
    await send_nudge(context, chat_id, 2, user_profile)

def start_nudge(context, chat_id, user_profile=None):
    if chat_id in nudge_tasks:
        nudge_tasks[chat_id].cancel()
    task = asyncio.create_task(nudge_sequence(context, chat_id, user_profile))
    nudge_tasks[chat_id] = task

def stop_nudge(chat_id):
    if chat_id in nudge_tasks:
        nudge_tasks[chat_id].cancel()
        del nudge_tasks[chat_id]

async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    stats = get_stats()
    if not stats:
        await update.message.reply_text("Gagal ngambil stats. Coba lagi ya!")
        return

    msg = f"""📊 *narAI Stats*

👥 Total users: {stats['total_users']}
🆕 New today: {stats['new_today']}

⚡ Actions sent: {stats['total_actions']}
✅ Completed: {stats['total_done']}
😩 Resisted: {stats['total_resist']}
📈 Completion rate: {stats['completion_rate']}%"""

    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_profile = get_user(chat_id)
    name = user_profile.get("name", "") if user_profile else ""

    stop_nudge(chat_id)

    if query.data == "done":
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        log_session(chat_id, user_last_action.get(chat_id, ""), "done")
        await query.edit_message_reply_markup(reply_markup=None)
        msg = f"Niceee{' ' + name if name else ''}, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?"
        await context.bot.send_message(chat_id=chat_id, text=msg)

    elif query.data == "resist":
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        log_session(chat_id, user_last_action.get(chat_id, ""), "resist")
        await query.edit_message_reply_markup(reply_markup=None)

        simplified = ask_narai_simplified(
            user_last_action.get(chat_id, "tugas lo"), level, user_profile
        )
        user_last_action[chat_id] = simplified
        user_state[chat_id] = ACTION_SENT

        await context.bot.send_message(
            chat_id=chat_id,
            text=simplified,
            reply_markup=action_buttons()
        )
        start_nudge(context, chat_id, user_profile)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text
    current_state = user_state.get(chat_id, None)

    stop_nudge(chat_id)

    user_profile = get_user(chat_id)

    # --- ONBOARDING FLOW ---
    if user_profile is None and current_state is None:
        user_state[chat_id] = ONBOARDING_NAME
        await update.message.reply_text(
            "Hai! Gue narAI 👋\n\nGue di sini buat bantu lo mulai ngerjain sesuatu pas lo lagi stuck, overwhelmed, atau capek.\n\nSebelum mulai, boleh kenalan dulu? *Nama lo siapa?*",
            parse_mode="Markdown"
        )
        return

    if current_state == ONBOARDING_NAME:
        context.user_data["name"] = user_message.strip()
        user_state[chat_id] = ONBOARDING_JOB
        await update.message.reply_text(
            f"Hai *{user_message.strip()}*! Seneng kenalan sama lo 😊\n\nLo kerja sebagai apa sekarang?",
            parse_mode="Markdown"
        )
        return

    if current_state == ONBOARDING_JOB:
        context.user_data["job"] = user_message.strip()
        user_state[chat_id] = ONBOARDING_STRUGGLE
        await update.message.reply_text(
            "Satu hal lagi — biasanya lo paling sering stuck di bagian mana dari kerjaan lo?"
        )
        return

    if current_state == ONBOARDING_STRUGGLE:
        name = context.user_data.get("name", "")
        job = context.user_data.get("job", "")
        struggle = user_message.strip()

        save_user(chat_id, name, job, struggle)
        user_profile = {"name": name, "job": job, "biggest_struggle": struggle}
        user_state[chat_id] = CLARIFYING

        await update.message.reply_text(
            f"Oke *{name}*, sekarang gue udah kenal lo! 🎉\n\nKapanpun lo stuck, overwhelmed, atau ga ada energi — tinggal cerita ke gue. Gue bakal kasih lo satu langkah kecil buat mulai.\n\nSo, sekarang lagi ngerjain apa?",
            parse_mode="Markdown"
        )
        return

    # --- NORMAL FLOW ---
    name = user_profile.get("name", "") if user_profile else ""

    if is_done(user_message) and user_state.get(chat_id) == ACTION_SENT:
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        log_session(chat_id, user_last_action.get(chat_id, ""), "done")
        msg = f"Niceee{' ' + name if name else ''}, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?"
        await update.message.reply_text(msg)
        return

    if is_resistance(user_message) and user_state.get(chat_id) == ACTION_SENT:
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        log_session(chat_id, user_last_action.get(chat_id, ""), "resist")
        simplified = ask_narai_simplified(user_last_action[chat_id], level, user_profile)
        user_last_action[chat_id] = simplified
        user_state[chat_id] = ACTION_SENT
        await update.message.reply_text(simplified, reply_markup=action_buttons())
        start_nudge(context, chat_id, user_profile)
        return

    classification = classify_message(user_message)

    if classification == "NO_TASK":
        user_state[chat_id] = CLARIFYING
        reply = ask_narai_clarify(user_message, user_profile)
        await update.message.reply_text(reply)
    else:
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = ACTION_SENT
        reply = ask_narai_action(user_message, user_profile)
        user_last_action[chat_id] = reply
        log_session(chat_id, reply, "sent")
        await update.message.reply_text(reply, reply_markup=action_buttons())
        check_in = f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥"
        await update.message.reply_text(check_in)
        start_nudge(context, chat_id, user_profile)

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)
    user_profile = get_user(chat_id)

    await update.message.reply_text("Bentar ya, gue liat dulu list lo... 👀")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_base64 = base64.b64encode(file_bytes).decode("utf-8")

    reply = ask_narai_from_image(image_base64, user_profile)
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT
    log_session(chat_id, reply, "sent")

    name = user_profile.get("name", "") if user_profile else ""
    await update.message.reply_text(reply, reply_markup=action_buttons())
    await update.message.reply_text(f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥")
    start_nudge(context, chat_id, user_profile)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)
    user_profile = get_user(chat_id)

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("Sekarang gue cuma bisa baca PDF sama gambar ya!")
        return

    await update.message.reply_text("Bentar ya, gue baca PDF lo dulu... 📄")

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()

    pdf = fitz.open(stream=bytes(file_bytes), filetype="pdf")
    text = ""
    for page in pdf:
        text += page.get_text()

    if not text.strip():
        await update.message.reply_text("Hmm PDF-nya kosong atau ga bisa dibaca. Coba kirim sebagai gambar aja!")
        return

    reply = ask_narai_from_list(text[:2000], user_profile)
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT
    log_session(chat_id, reply, "sent")

    name = user_profile.get("name", "") if user_profile else ""
    await update.message.reply_text(reply, reply_markup=action_buttons())
    await update.message.reply_text(f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥")
    start_nudge(context, chat_id, user_profile)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("stats", handle_stats))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
app.add_handler(CallbackQueryHandler(handle_button))

print("narAI is running...")
app.run_polling()
