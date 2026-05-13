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
user_language = {}

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

def detect_language(message):
    """Detect if message is English or Indonesian"""
    english_words = ["i", "the", "you", "can", "what", "how", "is", "are", "my", "me",
                     "need", "want", "help", "have", "do", "in", "to", "a", "and", "it",
                     "speak", "english", "please", "working", "task", "stuck", "overwhelmed"]
    words = message.lower().split()
    english_count = sum(1 for w in words if w in english_words)
    if english_count >= 1 or any(c.isascii() and c.isalpha() for c in message):
        non_indonesian = not any(word in message.lower() for word in
                                  ["gue", "lo", "aku", "kamu", "yang", "dengan", "untuk",
                                   "adalah", "dan", "ini", "itu", "tidak", "bisa", "mau",
                                   "lagi", "sama", "udah", "banget", "aja", "deh", "sih"])
        if english_count >= 2 or (english_count >= 1 and non_indonesian):
            return "EN"
    return "ID"

def get_language_instruction(lang):
    if lang == "EN":
        return "IMPORTANT: The user is writing in English. You MUST reply in English. Use casual, friendly English. Do NOT use Bahasa Indonesia at all."
    else:
        return "IMPORTANT: The user is writing in Bahasa Indonesia. You MUST reply in casual Bahasa Indonesia using lo/gue. Do NOT use English."

def is_resistance(message):
    message = message.lower()
    return any(word in message for word in RESISTANCE_WORDS)

def classify_user_intent(user_message, last_action, current_state):
    context_str = f"Last action narAI gave: {last_action}" if last_action else "No previous action."
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""You are an intent classifier for narAI.

{context_str}
Current state: {current_state}

Classify the user message. Reply with ONLY one word:

- DONE → user confirms they completed the task (e.g. "done", "finished", "udah selesai", "kelar", "beres")
- RESIST → user is avoiding or resisting the task (e.g. "males", "ga mau", "later", "too hard")
- NEW_TASK → user mentions a specific new task or work item
- QUESTION → user is asking something, requesting info or advice (look for question marks, words like "apakah", "gimana", "can you", "how", "what", "bisa bantu")
- NO_TASK → user is stuck/overwhelmed but hasn't mentioned a specific task

CRITICAL: If the message contains a question mark OR question words (apakah, gimana, can you, how, what, kenapa, bagaimana, bisa bantu, tolong), classify as QUESTION not DONE."""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=10
    )
    result = response.choices[0].message.content.strip().upper()
    for intent in ["DONE", "RESIST", "NEW_TASK", "QUESTION", "NO_TASK"]:
        if intent in result:
            return intent
    return "NEW_TASK"

def ask_narai_clarify(user_message, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"User's name is {name}." if name else ""
    lang_instruction = get_language_instruction(lang)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""You are narAI, a casual friendly coach that helps users start working. {name_str}
{lang_instruction}

Ask ONE short question to help the user identify one specific task.
Max 2 sentences. Casual and warm. Don't give action yet."""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=100
    )
    return response.choices[0].message.content

def ask_narai_action(user_message, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    struggle = user_profile.get("biggest_struggle", "") if user_profile else ""
    lang_instruction = get_language_instruction(lang)

    context_str = ""
    if name:
        context_str += f"User's name is {name}. "
    if struggle:
        context_str += f"They usually struggle with: {struggle}. "

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""You are narAI, a casual friendly coach that helps users start working immediately. {context_str}
{lang_instruction}

Rules:
- Give ONE very specific first step they can do RIGHT NOW
- Max 2 sentences — be concise
- Must be doable in under 10 minutes
- Use their name if you know it
- End with a small encouraging push
- Be specific to what they mentioned, not generic"""
            },
            {"role": "user", "content": user_message}
        ],
        max_tokens=80
    )
    return response.choices[0].message.content

def ask_narai_answer_question(user_message, last_action, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"User's name is {name}." if name else ""
    lang_instruction = get_language_instruction(lang)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""You are narAI, a casual friendly coach. {name_str}
{lang_instruction}

The user asked a question. Answer it briefly and helpfully, then redirect to one small action.

Rules:
- Answer their question first (1-2 sentences)
- Then suggest one small next step based on context
- Max 3 sentences total
- Casual and warm"""
            },
            {
                "role": "user",
                "content": f"Previous task context: {last_action}\n\nUser's question: {user_message}"
            }
        ],
        max_tokens=120
    )
    return response.choices[0].message.content

def ask_narai_simplified(last_action, resistance_level, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"User's name is {name}." if name else ""
    lang_instruction = get_language_instruction(lang)

    if resistance_level == 1:
        prompt = f"""{name_str} User is resisting this task: {last_action}
Make a SMALLER version of the task. 1-2 sentences. Casual."""
    else:
        prompt = f"""{name_str} User is still resisting. Previous task: {last_action}
Make the SMALLEST possible version — like "just open the app". 1-2 sentences. Casual."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"You are narAI, a casual friendly coach. {lang_instruction}"
            },
            {"role": "user", "content": prompt}
        ],
        max_tokens=80
    )
    return response.choices[0].message.content

def ask_narai_from_image(image_base64, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"User's name is {name}." if name else ""
    lang_instruction = get_language_instruction(lang)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"""You are narAI, a casual friendly coach. {name_str}
{lang_instruction}
User sent an image with their task list.
- Read everything in the image
- Pick ONE most concrete or easiest task
- Give one specific first step
- Max 2 sentences, casual
- Use their name if you know it
- End with a small push"""
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                    }
                ]
            }
        ],
        max_tokens=120
    )
    return response.choices[0].message.content

def ask_narai_from_list(content_text, user_profile=None, lang="ID"):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f"User's name is {name}." if name else ""
    lang_instruction = get_language_instruction(lang)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"You are narAI, a casual friendly coach. {name_str} {lang_instruction}"
            },
            {
                "role": "user",
                "content": f"""User sent their task list:

{content_text}

Pick ONE most concrete task. Give one specific first step. Max 2 sentences. Casual. Use their name if you know it. End with a small push."""
            }
        ],
        max_tokens=120
    )
    return response.choices[0].message.content

async def send_nudge(context, chat_id, nudge_number, user_profile=None):
    name = user_profile.get("name", "") if user_profile else ""
    name_str = f" {name}" if name else ""
    lang = user_language.get(chat_id, "ID")

    if lang == "EN":
        if nudge_number == 1:
            text = f"Hey{name_str}, how's it going? Did you get started? 👀"
        else:
            text = f"No worries{name_str}, just try 2 minutes. You don't need to finish — just start. 💪"
    else:
        if nudge_number == 1:
            text = f"Eh{name_str}, gimana? Udah mulai belum? 👀"
        else:
            text = f"Gapapa{name_str}, coba 2 menit aja deh. Ga perlu selesai, yang penting mulai. 💪"

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
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

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_profile = get_user(chat_id)
    if user_profile:
        name = user_profile.get("name", "")
        user_state[chat_id] = CLARIFYING
        await update.message.reply_text(
            f"Hai lagi *{name}*! 👋 Gue narAI, masih di sini buat lo.\n\nSekarang lagi ngerjain apa? Cerita aja.",
            parse_mode="Markdown"
        )
    else:
        user_state[chat_id] = ONBOARDING_NAME
        await update.message.reply_text(
            "Hai! Gue narAI 👋\n\nGue di sini buat bantu lo mulai ngerjain sesuatu pas lo lagi stuck, overwhelmed, atau capek.\n\nSebelum mulai, boleh kenalan dulu? *Nama lo siapa?*",
            parse_mode="Markdown"
        )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cara pakai narAI gampang banget:\n\n"
        "1️⃣ Ceritain apa yang lagi lo kerjain atau rasain\n"
        "2️⃣ narAI kasih lo SATU langkah kecil\n"
        "3️⃣ Tap *Udah done!* kalau selesai, atau *Males ah* kalau butuh versi lebih gampang\n\n"
        "Lo juga bisa kirim foto list tugas lo dan narAI bakal bacain dan pilihkan satu buat lo.\n\n"
        "Gampang kan? Yuk gas! 🔥",
        parse_mode="Markdown"
    )

async def handle_stuck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_profile = get_user(chat_id)
    lang = user_language.get(chat_id, "ID")
    user_state[chat_id] = CLARIFYING
    reply = ask_narai_clarify("I'm stuck and don't know where to start" if lang == "EN" else "gue stuck ga tau mau mulai dari mana", user_profile, lang)
    await update.message.reply_text(reply)

async def handle_overwhelmed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_profile = get_user(chat_id)
    lang = user_language.get(chat_id, "ID")
    user_state[chat_id] = CLARIFYING
    reply = ask_narai_clarify("I'm overwhelmed with too many tasks" if lang == "EN" else "gue overwhelmed banyak banget kerjaan", user_profile, lang)
    await update.message.reply_text(reply)

async def handle_energy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_profile = get_user(chat_id)
    lang = user_language.get(chat_id, "ID")
    name = user_profile.get("name", "") if user_profile else ""
    user_state[chat_id] = ACTION_SENT
    if lang == "EN":
        reply = f"That's okay{' ' + name if name else ''}. Just open your laptop and look at one easy task. Just 2 minutes, that's enough."
    else:
        reply = f"Oke{' ' + name if name else ''}, ga apa-apa. Coba buka laptop lo dan liat satu task yang paling gampang. 2 menit aja, itu cukup. Coba deh."
    user_last_action[chat_id] = reply
    await update.message.reply_text(reply, reply_markup=action_buttons())
    check_in = f"I'll check in with you in about an hour{' ' + name if name else ''}. Let's go! 🔥" if lang == "EN" else f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥"
    await update.message.reply_text(check_in)
    start_nudge(context, chat_id, user_profile)

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
    lang = user_language.get(chat_id, "ID")
    stop_nudge(chat_id)

    if query.data == "done":
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        log_session(chat_id, user_last_action.get(chat_id, ""), "done")
        await query.edit_message_reply_markup(reply_markup=None)
        if lang == "EN":
            msg = f"Niceee{' ' + name if name else ''}, I'm proud of you! 🙌 Want to keep going with the next thing?"
        else:
            msg = f"Niceee{' ' + name if name else ''}, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?"
        await context.bot.send_message(chat_id=chat_id, text=msg)

    elif query.data == "resist":
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        log_session(chat_id, user_last_action.get(chat_id, ""), "resist")
        await query.edit_message_reply_markup(reply_markup=None)
        simplified = ask_narai_simplified(
            user_last_action.get(chat_id, "the task"), level, user_profile, lang
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

    # Detect and store language
    lang = detect_language(user_message)
    user_language[chat_id] = lang

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

    # --- SMART INTENT DETECTION ---
    name = user_profile.get("name", "") if user_profile else ""
    last_action = user_last_action.get(chat_id, "")
    intent = classify_user_intent(user_message, last_action, current_state)

    if intent == "DONE" and current_state == ACTION_SENT:
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = CLARIFYING
        log_session(chat_id, last_action, "done")
        if lang == "EN":
            msg = f"Niceee{' ' + name if name else ''}, I'm proud of you! 🙌 Want to keep going with the next thing?"
        else:
            msg = f"Niceee{' ' + name if name else ''}, gue bangga sama lo! 🙌 Mau lanjut ke hal berikutnya?"
        await update.message.reply_text(msg)

    elif intent == "RESIST" and current_state == ACTION_SENT:
        level = user_resistance_level.get(chat_id, 0) + 1
        user_resistance_level[chat_id] = min(level, 2)
        log_session(chat_id, last_action, "resist")
        simplified = ask_narai_simplified(last_action, level, user_profile, lang)
        user_last_action[chat_id] = simplified
        user_state[chat_id] = ACTION_SENT
        await update.message.reply_text(simplified, reply_markup=action_buttons())
        start_nudge(context, chat_id, user_profile)

    elif intent == "QUESTION":
        reply = ask_narai_answer_question(user_message, last_action, user_profile, lang)
        await update.message.reply_text(reply)

    elif intent == "NO_TASK":
        user_state[chat_id] = CLARIFYING
        reply = ask_narai_clarify(user_message, user_profile, lang)
        await update.message.reply_text(reply)

    else:
        user_resistance_level[chat_id] = 0
        user_state[chat_id] = ACTION_SENT
        reply = ask_narai_action(user_message, user_profile, lang)
        user_last_action[chat_id] = reply
        log_session(chat_id, reply, "sent")
        await update.message.reply_text(reply, reply_markup=action_buttons())
        if lang == "EN":
            check_in = f"I'll check in with you in about an hour{' ' + name if name else ''}. Let's go! 🔥"
        else:
            check_in = f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥"
        await update.message.reply_text(check_in)
        start_nudge(context, chat_id, user_profile)

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)
    user_profile = get_user(chat_id)
    lang = user_language.get(chat_id, "ID")
    name = user_profile.get("name", "") if user_profile else ""

    if lang == "EN":
        await update.message.reply_text("Hold on, let me check your list... 👀")
    else:
        await update.message.reply_text("Bentar ya, gue liat dulu list lo... 👀")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_base64 = base64.b64encode(file_bytes).decode("utf-8")
    reply = ask_narai_from_image(image_base64, user_profile, lang)
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT
    log_session(chat_id, reply, "sent")

    await update.message.reply_text(reply, reply_markup=action_buttons())
    if lang == "EN":
        await update.message.reply_text(f"I'll check in with you in about an hour{' ' + name if name else ''}. Let's go! 🔥")
    else:
        await update.message.reply_text(f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥")
    start_nudge(context, chat_id, user_profile)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stop_nudge(chat_id)
    user_profile = get_user(chat_id)
    lang = user_language.get(chat_id, "ID")
    name = user_profile.get("name", "") if user_profile else ""

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        msg = "I can only read PDFs and images right now!" if lang == "EN" else "Sekarang gue cuma bisa baca PDF sama gambar ya!"
        await update.message.reply_text(msg)
        return

    msg = "Hold on, reading your PDF... 📄" if lang == "EN" else "Bentar ya, gue baca PDF lo dulu... 📄"
    await update.message.reply_text(msg)

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    pdf = fitz.open(stream=bytes(file_bytes), filetype="pdf")
    text = ""
    for page in pdf:
        text += page.get_text()

    if not text.strip():
        msg = "Hmm, the PDF seems empty or unreadable. Try sending it as an image!" if lang == "EN" else "Hmm PDF-nya kosong atau ga bisa dibaca. Coba kirim sebagai gambar aja!"
        await update.message.reply_text(msg)
        return

    reply = ask_narai_from_list(text[:2000], user_profile, lang)
    user_last_action[chat_id] = reply
    user_resistance_level[chat_id] = 0
    user_state[chat_id] = ACTION_SENT
    log_session(chat_id, reply, "sent")

    await update.message.reply_text(reply, reply_markup=action_buttons())
    if lang == "EN":
        await update.message.reply_text(f"I'll check in with you in about an hour{' ' + name if name else ''}. Let's go! 🔥")
    else:
        await update.message.reply_text(f"Gue bakal check in sama lo sekitar 1 jam lagi ya{' ' + name if name else ''}. Gas! 🔥")
    start_nudge(context, chat_id, user_profile)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", handle_start))
app.add_handler(CommandHandler("help", handle_help))
app.add_handler(CommandHandler("stuck", handle_stuck_cmd))
app.add_handler(CommandHandler("overwhelmed", handle_overwhelmed_cmd))
app.add_handler(CommandHandler("energy", handle_energy_cmd))
app.add_handler(CommandHandler("stats", handle_stats))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
app.add_handler(CallbackQueryHandler(handle_button))

print("narAI is running...")
app.run_polling()
