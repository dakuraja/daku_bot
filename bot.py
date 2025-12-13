import time
import requests
import logging
import random
import os
import json
from dotenv import load_dotenv

# Supabase Client
from supabase import create_client, Client

# PDF Support
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# --- SQLite persistence for questions (added by assistant) ---
import sqlite3
from pathlib import Path as _Path

BASE_DIR = _Path(__file__).parent
DB_PATH = BASE_DIR / "questions.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT COLLATE NOCASE,
        question TEXT,
        option1 TEXT,
        option2 TEXT,
        option3 TEXT,
        option4 TEXT,
        correct INTEGER,
        explanation TEXT
    )
    """)
    conn.commit()
    conn.close()

def db_add_question(topic, question, opts, correct, explanation):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO questions (topic, question, option1, option2, option3, option4, correct, explanation)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (topic, question, opts[0], opts[1], opts[2], opts[3], int(correct), explanation))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid

def db_get_topics():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT topic FROM questions ORDER BY topic COLLATE NOCASE")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

def db_get_questions_by_topic(topic):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, topic, question, option1, option2, option3, option4, correct, explanation
        FROM questions WHERE topic = ? COLLATE NOCASE
        ORDER BY id
    """, (topic,))
    rows = cur.fetchall()
    conn.close()
    qlist = []
    for r in rows:
        qlist.append({
            "id": r[0],
            "topic": r[1],
            "question": r[2],
            "options": [r[3], r[4], r[5], r[6]],
            # convert DB correct (1..4) to 0-based for code that expects zero-based
            "correct": (r[7] - 1) if r[7] is not None else 0,
            "explanation": r[8] or ""
        })
    return qlist

def db_get_all_questions():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, topic, question, option1, option2, option3, option4, correct, explanation FROM questions ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    qlist = []
    for r in rows:
        qlist.append({
            "id": r[0],
            "topic": r[1],
            "question": r[2],
            "options": [r[3], r[4], r[5], r[6]],
            "correct": (r[7] - 1) if r[7] is not None else 0,
            "explanation": r[8] or ""
        })
    return qlist

# initialize DB file
init_db()
# --- end SQLite block ---



# ---------------- PATH SETUP ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_FILE = os.path.join(BASE_DIR, "questions.json")
LEADERBOARD_FILE = os.path.join(BASE_DIR, "leaderboard.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
RESULTS_HISTORY_FILE = os.path.join(BASE_DIR, "results.json")

FONTS_DIR = os.path.join(BASE_DIR, "fonts")
PDF_FONT_PATH = os.path.join(FONTS_DIR, "NotoSansDevanagari-Regular.ttf")

load_dotenv()


# ---------------- BOT TOKEN ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing. Set in Render Environment.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------------- SUPABASE CONFIG ----------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("❌ Supabase credentials missing.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- DEFAULT SETTINGS ----------------
QUESTION_TIME = 45
POLL_TIMEOUT = 20

MARK_CORRECT = 1.0
MARK_WRONG = -0.33

NEXT_Q_ID = 1
QUESTIONS = []

group_state = {}
leaderboard = {}
results_history = {}

# ---------------- QUIZ MASTER STATE (ADMIN ONLY) ----------------
QUIZ_RUNNING = False
QUIZ_PAUSED = False



# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("BPSC-Quiz-Bot")


# ---------------- TIMER BAR HELPERS ----------------
def build_timer_bar(remaining, total):
    if total <= 0:
        total = 1
    if remaining < 0:
        remaining = 0

    blocks = 10
    filled = int((remaining / total) * blocks)

    if filled < 0:
        filled = 0
    if filled > blocks:
        filled = blocks

    return "█" * filled + "░" * (10 - filled), filled


def format_timer_line(remaining, total):
    bar, _ = build_timer_bar(remaining, total)
    return f"⏳ {bar} {remaining}s"

# -------------------------------------------------
#   QUESTIONS PERSISTENCE (SUPABASE)
# -------------------------------------------------
def save_questions_to_db():
    """
    QUESTIONS list ko Supabase 'questions' table me sync karta hai.
    Simple तरीका: purani rows delete + nayi insert.
    """
    try:
        # purana sab delete
        supabase.table("questions").delete().neq("id", 0).execute()

        if not QUESTIONS:
            log.info("Supabase: QUESTIONS खाली है, table clear कर दी गई।")
            return

        rows = []
        for q in QUESTIONS:
            rows.append(
                {
                    "id": q.get("id"),
                    "topic": q.get("topic", "General"),
                    "question": q.get("question", ""),
                    "options": q.get("options", []),
                    "correct": q.get("correct", 0),
                    "explanation": q.get("explanation", ""),
                }
            )

        supabase.table("questions").insert(rows).execute()
        log.info("Supabase: %d questions sync हो गए।", len(rows))

    except Exception as e:
        log.error("Supabase save_questions_to_db error: %s", e)


def load_questions_from_db():
    """
    Supabase 'questions' table se QUESTIONS load karta hai
    aur NEXT_Q_ID set karta hai.
    """
    global QUESTIONS, NEXT_Q_ID

    try:
        res = supabase.table("questions").select("*").order("id", desc=False).execute()
        rows = res.data or []
    except Exception as e:
        log.error("Supabase load_questions_from_db error: %s", e)
        rows = []

    QUESTIONS = []
    max_id = 0

    for row in rows:
        q_id = row.get("id")
        topic = row.get("topic") or "General"
        question = row.get("question") or ""
        options = row.get("options") or []
        correct = row.get("correct") or 0
        explanation = row.get("explanation") or ""

        if not isinstance(options, list) or len(options) != 4:
            continue

        try:
            q_id_int = int(q_id)
        except Exception:
            continue

        QUESTIONS.append(
            {
                "id": q_id_int,
                "topic": topic,
                "question": question,
                "options": options,
                "correct": int(correct),
                "explanation": explanation,
            }
        )
        max_id = max(max_id, q_id_int)

    NEXT_Q_ID = max_id + 1 if max_id > 0 else 1
    log.info("Supabase se %d questions load hue. NEXT_Q_ID=%s", len(QUESTIONS), NEXT_Q_ID)
# ------------ Backward compatibility wrappers -------------
# Purane file-based function names ko Supabase wale functions se map karte hain

def load_questions_from_file():
    """Purana function — ab DB se hi load karega"""
    load_questions_from_db()


def save_questions_to_file():
    """Purana function — ab DB me hi save karega"""
    save_questions_to_db()


# -------------------------------------------------
#   LEADERBOARD / HISTORY JSON (LOCAL FILES)
# -------------------------------------------------
def save_leaderboard_to_file():
    try:
        to_save = {}
        for chat_id, users in leaderboard.items():
            chat_key = str(chat_id)
            to_save[chat_key] = {}
            for uid, data in users.items():
                to_save[chat_key][str(uid)] = data
        with open(LEADERBOARD_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        log.info("leaderboard.json update किया गया।")
    except Exception as e:
        log.error("leaderboard.json save error: %s", e)


def load_leaderboard_from_file():
    global leaderboard

    if not os.path.exists(LEADERBOARD_FILE):
        save_leaderboard_to_file()
        return

    try:
        with open(LEADERBOARD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                tmp = {}
                for chat_id_str, users in data.items():
                    try:
                        chat_id = int(chat_id_str)
                    except ValueError:
                        continue
                    tmp[chat_id] = {}
                    if isinstance(users, dict):
                        for user_id_str, udata in users.items():
                            try:
                                uid = int(user_id_str)
                            except ValueError:
                                continue
                            tmp[chat_id][uid] = udata
                leaderboard = tmp
                log.info("leaderboard.json से data load हुआ।")
    except Exception as e:
        log.error("leaderboard.json load error: %s", e)


def save_results_history_to_file():
    try:
        to_save = {}
        for chat_id, records in results_history.items():
            to_save[str(chat_id)] = records
        with open(RESULTS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        log.info("results.json update किया गया।")
    except Exception as e:
        log.error("results.json save error: %s", e)


def load_results_history_from_file():
    global results_history

    if not os.path.exists(RESULTS_HISTORY_FILE):
        save_results_history_to_file()
        return

    try:
        with open(RESULTS_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                tmp = {}
                for chat_id_str, records in data.items():
                    try:
                        chat_id = int(chat_id_str)
                    except ValueError:
                        continue
                    if isinstance(records, list):
                        tmp[chat_id] = records
                results_history = tmp
                log.info("results.json से data load हुआ।")
    except Exception as e:
        log.error("results.json load error: %s", e)


# ---------------- SETTINGS (QUESTION TIME) ----------------
def save_settings():
    try:
        data = {"QUESTION_TIME": QUESTION_TIME}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("settings.json update किया गया (QUESTION_TIME=%s).", QUESTION_TIME)
    except Exception as e:
        log.error("settings.json save error: %s", e)


def load_settings():
    global QUESTION_TIME
    if not os.path.exists(SETTINGS_FILE):
        log.info("settings.json नहीं मिला, default QUESTION_TIME=%s से नई फाइल बना रहे हैं.", QUESTION_TIME)
        save_settings()
        return

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "QUESTION_TIME" in data:
            qt = int(data["QUESTION_TIME"])
            if 5 <= qt <= 600:
                QUESTION_TIME = qt
        log.info("settings.json से QUESTION_TIME=%s load हुआ।", QUESTION_TIME)
    except Exception as e:
        log.error("settings.json load error: %s", e)
# ---------------- BASIC TELEGRAM FUNCTIONS ----------------
def api_call(method, params=None):
    try:
        r = requests.get(
            f"{API_URL}/{method}",
            params=params,
            timeout=POLL_TIMEOUT + 5,
        )
        return r.json()
    except Exception as e:
        log.error("API error (%s): %s", method, e)
        return None


def send_msg(chat_id, text, reply_markup=None, parse_mode=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        params["parse_mode"] = parse_mode
    return api_call("sendMessage", params)


def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        return api_call("editMessageText", params)
    except Exception as e:
        log.error("editMessageText error: %s", e)
        return None


def edit_reply_markup(chat_id, message_id, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api_call("editMessageReplyMarkup", params)


def send_document(chat_id, file_path, caption=None):
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            r = requests.post(
                f"{API_URL}/sendDocument",
                data=data,
                files=files,
                timeout=POLL_TIMEOUT + 5,
            )
            return r.json()
    except Exception as e:
        log.error("sendDocument error: %s", e)
        return None


def answer_callback(cb_id, text=""):
    api_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})


def get_chat_member(chat_id, user_id):
    data = api_call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    if data and data.get("ok"):
        return data["result"]
    return None


# ---------------- PERMISSION / HELPER ----------------
def is_admin(message):
    chat_type = message["chat"]["type"]
    user = message["from"]

    if chat_type == "private":
        return True

    member = get_chat_member(message["chat"]["id"], user["id"])
    return member and member["status"] in ("administrator", "creator")


def teacher_allowed(message):
    chat_type = message["chat"]["type"]
    if chat_type == "private":
        return True
    return is_admin(message)


def find_question_index_by_id(q_id):
    for idx, q in enumerate(QUESTIONS):
        if q.get("id") == q_id:
            return idx
    return -1


# ---------------- BASIC COMMANDS ----------------
def start_command(message):
    chat_id = message["chat"]["id"]
    text = (
        "नमस्ते! 👋\n"
        "मैं *BPSC IntelliQuiz Bot* हूँ — आपकी तैयारी का Smart साथी।\n\n"
        "🎯 लक्ष्य: कम समय में अधिक Revision\n"
        "📚 फ़ोकस: BPSC Prelims — History, Polity, Geography, Economy, Current Affairs\n"
        "⚡ मिशन: “Smart Practice, Better Accuracy, Final Selection!”\n\n"
        "🔹 *Student commands (Topic-wise Quiz):*\n"
        "• `/quiz` – Mixed topics, short (5 सवाल)\n"
        "• `/quiz short` – Mixed, 5 सवाल\n"
        "• `/quiz long` – Mixed, ~15 सवाल\n"
        "• `/quiz full` – Mixed, ~25 सवाल\n"
        "• `/quiz history short` – सिर्फ History (5 सवाल)\n"
        "• `/quiz history full` – सिर्फ History (25 सवाल तक)\n"
        "• `/quiz polity long` – सिर्फ Polity (~15 सवाल)\n\n"
        "• `/quiz_pause` – चल रहे quiz को pause करें (Admin only)"
        "• `/quiz_resume` – paused quiz को resume करें (Admin only)"
        "• /quiz_stop` – quiz को पूरी तरह stop करें (Admin only)"
        "🔹 *Leaderboard commands:*\n"
        "• `/leaderboard` – इस group का overall cumulative स्कोर\n"
        "• `/leaderboard_today` – आज का topic-mix स्कोर\n"
        "• `/leaderboard_week` – पिछले 7 दिनों का स्कोर\n"
        "• `/leaderboard_month` – पिछले 30 दिनों का स्कोर\n\n"
        "🔹 *Teacher/Admin commands:*\n"
        "• `/addq Topic | प्रश्न | A | B | C | D | सही (1-4) | व्याख्या`\n"
        "• `/bulkadd` + कई /addq lines\n"
        "• `/editq ID | नया प्रश्न | A | B | C | D | सही (1-4) | नई व्याख्या`\n"
        "• `/removeq ID` – सवाल हटाएँ\n"
        "• `/listq` – questions list (ID + preview)\n"
        "• `/exportq` – questions bank TXT file\n"
        "• `/exportpdf` – questions bank PDF file\n"
        "• `/settime 60` – हर सवाल का समय 60 सेकंड\n"
        "• `/resetboard` – leaderboard साफ़ करें\n\n"
        "_नोट: Students अपना detailed result bot की private chat में देख सकते हैं।_"
    )
    send_msg(chat_id, text, parse_mode="Markdown")


# ---------- /quiz args parsing: topic + mode ----------
def parse_quiz_args(text: str):
    parts = text.split()
    args = parts[1:]

    allowed_modes = {"short", "long", "full"}
    topic = None
    mode = "short"

    for a in args:
        al = a.lower()
        if al in allowed_modes:
            mode = al
        elif topic is None:
            topic = a

    return topic, mode


# ---------------- QUIZ START / FLOW ----------------

def quiz_pause(message):
    global QUIZ_PAUSED, QUIZ_RUNNING
    chat_id = message["chat"]["id"]
    if not is_admin(message):
        send_msg(chat_id, "⛔ केवल Admin quiz pause कर सकता है।")
        return
    if not QUIZ_RUNNING:
        send_msg(chat_id, "❌ कोई quiz चल नहीं रहा है।")
        return
    if QUIZ_PAUSED:
        send_msg(chat_id, "⏸ Quiz पहले से paused है।")
        return
    QUIZ_PAUSED = True
    send_msg(chat_id, "⏸ Quiz Admin द्वारा PAUSE कर दिया गया है।")


def quiz_resume(message):
    global QUIZ_PAUSED
    chat_id = message["chat"]["id"]
    if not is_admin(message):
        send_msg(chat_id, "⛔ केवल Admin quiz resume कर सकता है।")
        return
    if not QUIZ_PAUSED:
        send_msg(chat_id, "▶ Quiz paused नहीं है।")
        return
    QUIZ_PAUSED = False
    send_msg(chat_id, "▶ Quiz फिर से RESUME कर दिया गया है।")


def quiz_stop(message):
    global QUIZ_RUNNING, QUIZ_PAUSED
    chat_id = message["chat"]["id"]
    if not is_admin(message):
        send_msg(chat_id, "⛔ केवल Admin quiz stop कर सकता है।")
        return
    if not QUIZ_RUNNING:
        send_msg(chat_id, "❌ कोई quiz चल नहीं रहा है।")
        return
    QUIZ_RUNNING = False
    QUIZ_PAUSED = False
    group_state.pop(chat_id, None)
    send_msg(chat_id, "🛑 Quiz Admin द्वारा STOP कर दिया गया है।")

def start_quiz(message):
    global QUIZ_RUNNING, QUIZ_PAUSED
    chat_id = message["chat"]["id"]
    text = message.get("text", "") or ""

    if not is_admin(message):
        send_msg(chat_id, "केवल admin /quiz चला सकता है।")
        return

    if not QUESTIONS:
        send_msg(chat_id, "अभी कोई सवाल मौजूद नहीं है। पहले /addq या /bulkadd से सवाल जोड़ें।")
        return

    st_exist = group_state.get(chat_id)
    if st_exist and st_exist.get("q_index", 0) < len(st_exist.get("order", [])):
        send_msg(chat_id, "पहले वाला quiz अभी चल रहा है। उसके ख़त्म होने के बाद नया शुरू करें।")
        return

    topic_arg, mode = parse_quiz_args(text)
    total_available = len(QUESTIONS)

    topic_filter = None
    if topic_arg:
        topic_filter = topic_arg.strip().lower()

    if topic_filter:
        indices_all = [
            i for i, q in enumerate(QUESTIONS)
            if str(q.get("topic", "General")).strip().lower() == topic_filter
        ]
        if not indices_all:
            send_msg(
                chat_id,
                f"इस topic (`{topic_arg}`) के लिए अभी कोई सवाल नहीं है। पहले /addq से सवाल जोड़ें।"
            )
            return
        topic_label = topic_arg
    else:
        indices_all = list(range(total_available))
        topic_label = "Mixed (सभी topics)"

    desired_map = {"short": 5, "long": 15, "full": 25}
    if mode not in desired_map:
        mode = "short"
    desired = desired_map[mode]

    count = min(desired, len(indices_all))
    if count == 0:
        send_msg(chat_id, "अभी सवाल पर्याप्त नहीं हैं।")
        return

    order = indices_all[:]
    random.shuffle(order)
    order = order[:count]

    mode_label_map = {
        "short": "Short (5 Q)",
        "long": "Long (~15 Q)",
        "full": "Full Mock (~25 Q)",
    }
    mode_label = mode_label_map.get(mode, mode)

    QUIZ_RUNNING = True
    QUIZ_PAUSED = False

    group_state[chat_id] = {
        "order": order,
        "q_index": 0,
        "start": time.time(),
        "answers": {},
        "user_stats": {},
        "msg_id": None,
        "topic": topic_label if topic_filter else "Mixed",
        "last_timer_update": 0,
    }

    send_msg(
        chat_id,
        (
            "🎯 Quiz शुरू!\n"
            f"Mode: {mode_label}\n"
            f"Topic: {topic_label}\n"
            f"Questions: {len(order)}\n"
            f"हर सवाल का समय: {QUESTION_TIME} सेकंड\n"
            f"Marking: सही = {MARK_CORRECT}, गलत = {MARK_WRONG}\n"
            "आपका detailed result आपको private chat में भेजा जाएगा।"
        ),
    )

    send_question(chat_id)


def build_question_text(q, q_number, total_q, remaining):
    header = f"📝 सवाल {q_number}/{total_q} (कुल समय: {QUESTION_TIME} सेकंड)\n"
    timer_line = format_timer_line(remaining, QUESTION_TIME)
    body = q["question"]
    return header + timer_line + "\n\n" + body


def send_question(chat_id):
    if QUIZ_PAUSED or not QUIZ_RUNNING:
        return
    st = group_state.get(chat_id)
    if not st:
        return

    order = st["order"]
    q_idx = st["q_index"]
    if q_idx >= len(order):
        return

    q = QUESTIONS[order[q_idx]]
    qid = q.get("id")

    buttons = [
        [{"text": opt, "callback_data": f"ans|{qid}|{i}"}]
        for i, opt in enumerate(q["options"])
    ]
    markup = {"inline_keyboard": buttons}

    text = build_question_text(q, q_idx + 1, len(order), QUESTION_TIME)
    res = send_msg(chat_id, text, reply_markup=markup)

    if res and res.get("ok"):
        try:
            st["msg_id"] = res["result"]["message_id"]
        except Exception:
            st["msg_id"] = None

    st["start"] = time.time()
    st["last_timer_update"] = 0
    st["answers"] = {}


def update_timer_for_chat(chat_id, now):
    st = group_state.get(chat_id)
    if not st:
        return

    msg_id = st.get("msg_id")
    start_time = st.get("start")
    if not msg_id or not start_time:
        return

    elapsed = now - start_time
    remaining = QUESTION_TIME - int(elapsed)

    if remaining <= 0:
        return

    # update interval logic
    if remaining > 25:
        min_delta = 5
    elif remaining > 10:
        min_delta = 3
    else:
        min_delta = 2

    last_upd = st.get("last_timer_update", 0)
    if last_upd and (now - last_upd) < min_delta:
        return

    order = st["order"]
    q_idx = st["q_index"]
    if q_idx >= len(order):
        return

    q = QUESTIONS[order[q_idx]]
    new_text = build_question_text(q, q_idx + 1, len(order), remaining)

    buttons = [
        [{"text": opt, "callback_data": f"ans|{q.get('id')}|{i}"}]
        for i, opt in enumerate(q["options"])
    ]
    markup = {"inline_keyboard": buttons}

    edit_message_text(chat_id, msg_id, new_text, reply_markup=markup)
    st["last_timer_update"] = now


def timeout_check():
    now = time.time()
    for chat_id, st in list(group_state.items()):
        if QUIZ_PAUSED or not QUIZ_RUNNING:
            continue
        start_time = st.get("start")
        if not start_time:
            continue

        # pehle timer update karo
        update_timer_for_chat(chat_id, now)

        # phir time over check
        if now - start_time >= QUESTION_TIME:
            finish_question(chat_id)

# ---------------- QUESTION FINISH / SUMMARY ----------------
def finish_question(chat_id):
    st = group_state.get(chat_id)
    if not st:
        return

    order = st["order"]
    q_idx = st["q_index"]
    if q_idx >= len(order):
        return

    msg_id = st.get("msg_id")
    if msg_id:
        # options वाले buttons हटा दो
        edit_reply_markup(chat_id, msg_id)

    q = QUESTIONS[order[q_idx]]
    correct = q["correct"]

    summary = (
        "⏰ समय समाप्त!\n"
        f"✅ सही उत्तर: {q['options'][correct]}\n\n"
        f"ℹ️ व्याख्या:\n{q['explanation']}"
    )
    send_msg(chat_id, summary)

    st["q_index"] += 1

    if st["q_index"] < len(order):
        send_question(chat_id)
    else:
        send_msg(chat_id, "🎉 Quiz खत्म! नीचे Leaderboard और आपकी summary भेजी जा रही है…")
        send_user_summaries(chat_id)
        send_leaderboard(chat_id)


# ---------------- ANSWER HANDLING ----------------
def handle_answer(cb):
    user = cb["from"]
    user_id = user["id"]
    message = cb.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    data = cb.get("data", "")
    cb_id = cb["id"]

    if not chat_id:
        answer_callback(cb_id, "Error: chat नहीं मिला।")
        return

    st = group_state.get(chat_id)
    if not st:
        answer_callback(cb_id, "अभी कोई quiz active नहीं है।")
        return

    if QUIZ_PAUSED:
        answer_callback(cb_id, "⏸ Quiz अभी paused है।")
        return

    if time.time() - st.get("start", 0) > QUESTION_TIME:
        answer_callback(cb_id, "इस सवाल का समय समाप्त हो चुका है।")
        return

    try:
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "ans":
            answer_callback(cb_id, "Invalid answer.")
            return
        qid = int(parts[1])
        selected = int(parts[2])
    except Exception:
        answer_callback(cb_id, "Invalid answer format.")
        return

    order = st["order"]
    q_idx = st["q_index"]
    if q_idx >= len(order):
        answer_callback(cb_id, "Quiz समाप्त हो चुका है।")
        return

    q = QUESTIONS[order[q_idx]]
    current_qid = q.get("id")
    if qid != current_qid:
        answer_callback(cb_id, "यह सवाल अब active नहीं है (पुराना message हो सकता है)।")
        return

    if user_id in st["answers"]:
        answer_callback(cb_id, "आप पहले ही इस सवाल का जवाब दे चुके हैं।")
        return

    correct = q["correct"]
    is_right = (selected == correct)

    # quiz session stats
    stats = st.setdefault("user_stats", {})
    u_stats = stats.get(user_id, {"correct": 0, "wrong": 0, "attempted": 0})
    u_stats["attempted"] += 1
    if is_right:
        u_stats["correct"] += 1
    else:
        u_stats["wrong"] += 1
    stats[user_id] = u_stats

    # leaderboard (cumulative)
    board = leaderboard.setdefault(chat_id, {})
    name = (user.get("first_name") or "") + " " + (user.get("last_name") or "")
    name = name.strip() or user.get("username") or str(user_id)

    prev = board.get(user_id, {"name": name, "score": 0.0})
    if is_right:
        prev["score"] += MARK_CORRECT
    else:
        prev["score"] += MARK_WRONG
    prev["name"] = name
    board[user_id] = prev

    save_leaderboard_to_file()

    st["answers"][user_id] = True

    status_text = "✔ सही" if is_right else "❌ गलत"
    dm_text = (
        f"सवाल: {q['question']}\n"
        f"आपका जवाब: {q['options'][selected]}\n"
        f"{status_text}\n\n"
        f"ℹ️ व्याख्या:\n{q['explanation']}"
    )
    dm_res = send_msg(user_id, dm_text)
    if not dm_res or not dm_res.get("ok"):
        log.info("User %s को DM नहीं भेज पाए (शायद user ने bot को private में start नहीं किया).", user_id)

    answer_callback(cb_id, "जवाब दर्ज किया गया!")


# ---------------- SUMMARY + LEADERBOARD ----------------
def send_user_summaries(chat_id):
    st = group_state.get(chat_id)
    if not st:
        return

    stats = st.get("user_stats", {})
    board = leaderboard.get(chat_id, {})
    total_q = len(st["order"])
    topic_label = st.get("topic", "Mixed")

    records_to_add = []
    now_ts = int(time.time())

    for user_id, u_stats in stats.items():
        correct = u_stats.get("correct", 0)
        wrong = u_stats.get("wrong", 0)
        attempted = u_stats.get("attempted", 0)
        skipped = total_q - attempted

        quiz_score = correct * MARK_CORRECT + wrong * MARK_WRONG

        total_score = 0.0
        name = str(user_id)
        if user_id in board:
            total_score = board[user_id].get("score", 0.0)
            name = board[user_id].get("name", name)

        summary_text = (
            "📊 आपका Quiz Summary:\n\n"
            f"Topic: {topic_label}\n"
            f"कुल प्रश्न: {total_q}\n"
            f"सही: {correct}\n"
            f"गलत: {wrong}\n"
            f"नहीं किए: {skipped}\n\n"
            f"इस quiz का score (नेगेटिव मार्किंग सहित): {quiz_score:.2f}\n"
            f"Overall leaderboard score: {total_score:.2f}\n"
        )

        send_msg(user_id, summary_text)

        records_to_add.append(
            {
                "user_id": user_id,
                "name": name,
                "score": float(quiz_score),
                "ts": now_ts,
                "topic": topic_label,
            }
        )

    if records_to_add:
        hist = results_history.setdefault(chat_id, [])
        hist.extend(records_to_add)
        save_results_history_to_file()


def send_leaderboard(chat_id):
    board = leaderboard.get(chat_id, {})
    if not board:
        send_msg(chat_id, "अभी कोई स्कोर नहीं है।")
        return

    sorted_board = sorted(board.items(), key=lambda x: x[1]["score"], reverse=True)

    text = "🏆 *Overall Leaderboard* (नेगेटिव मार्किंग सहित)\n\n"
    for rank, (uid, data) in enumerate(sorted_board, 1):
        text += f"{rank}. {data['name']} — {data['score']:.2f}\n"

    send_msg(chat_id, text, parse_mode="Markdown")


def show_leaderboard(message):
    chat_id = message["chat"]["id"]
    send_leaderboard(chat_id)


# ---------------- TIME-BASED LEADERBOARD ----------------
def build_time_leaderboard(chat_id, days, title):
    hist = results_history.get(chat_id, [])
    if not hist:
        return f"{title}\n\nअभी तक किसी ने भी क्विज नहीं दिया है।"

    now_ts = int(time.time())
    cutoff = now_ts - days * 86400 if days is not None else None

    agg = {}  # user_id -> {"name": str, "score": float}
    for rec in hist:
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if cutoff is not None and ts < cutoff:
            continue

        uid = rec.get("user_id")
        name = rec.get("name") or str(uid)
        score = float(rec.get("score", 0.0))

        data = agg.get(uid)
        if not data:
            data = {"name": name, "score": 0.0}
        data["score"] += score
        data["name"] = name
        agg[uid] = data

    if not agg:
        if days == 1:
            return f"{title}\n\nआज किसी ने भी क्विज नहीं दिया।"
        elif days == 7:
            return f"{title}\n\nपिछले 7 दिनों में किसी ने भी क्विज नहीं दिया।"
        elif days == 30:
            return f"{title}\n\nपिछले 30 दिनों में किसी ने भी क्विज नहीं दिया।"
        else:
            return f"{title}\n\nडेटा उपलब्ध नहीं है।"

    sorted_users = sorted(agg.values(), key=lambda x: x["score"], reverse=True)

    lines = [title, ""]
    for rank, data in enumerate(sorted_users[:20], start=1):
        lines.append(f"{rank}. {data['name']} — {data['score']:.2f}")

    return "\n".join(lines)


def handle_leaderboard_today(message):
    chat_id = message["chat"]["id"]
    text = build_time_leaderboard(chat_id, 1, "📅 आज का Leaderboard")
    send_msg(chat_id, text)


def handle_leaderboard_week(message):
    chat_id = message["chat"]["id"]
    text = build_time_leaderboard(chat_id, 7, "📆 पिछले 7 दिनों का Leaderboard")
    send_msg(chat_id, text)


def handle_leaderboard_month(message):
    chat_id = message["chat"]["id"]
    text = build_time_leaderboard(chat_id, 30, "🗓 पिछले 30 दिनों का Leaderboard")
    send_msg(chat_id, text)


# ---------------- TEACHER COMMANDS ----------------
def handle_addq(message):
    global NEXT_Q_ID

    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "")
    content = text[len("/addq"):].strip()
    parts = [p.strip() for p in content.split("|")]

    # 2 format support:
    # 1) OLD:   प्रश्न | A | B | C | D | सही | व्याख्या   (no topic)
    # 2) NEW:   Topic | प्रश्न | A | B | C | D | सही | व्याख्या
    if len(parts) < 7:
        send_msg(
            message["chat"]["id"],
            "फॉर्मेट गलत है.\nनया format:\n"
            "/addq Topic | प्रश्न | Option A | Option B | Option C | Option D | 2 | व्याख्या\n\n"
            "पुराना format भी चलेगा (topic = General):\n"
            "/addq प्रश्न | Option A | Option B | Option C | Option D | 2 | व्याख्या"
        )
        return

    if len(parts) == 7:
        topic = "General"
        question = parts[0]
        options = parts[1:5]
        correct_str = parts[5]
        explanation = parts[6]
    else:
        topic = parts[0] or "General"
        question = parts[1]
        options = parts[2:6]
        correct_str = parts[6]
        explanation = parts[7]

    if len(options) != 4:
        send_msg(message["chat"]["id"], "आपको 4 options देने हैं (A, B, C, D).")
        return

    try:
        correct_num = int(correct_str)
    except ValueError:
        send_msg(message["chat"]["id"], "सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए।")
        return

    if not 1 <= correct_num <= 4:
        send_msg(message["chat"]["id"], "सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए।")
        return

    entry = {
        "id": NEXT_Q_ID,
        "topic": topic,
        "question": question,
        "options": options,
        "correct": correct_num - 1,
        "explanation": explanation,
    }

    QUESTIONS.append(entry)
    q_id = NEXT_Q_ID
    NEXT_Q_ID += 1
    save_questions_to_file()

    send_msg(
        message["chat"]["id"],
        f"✅ नया सवाल जोड़ दिया गया है। (ID: {q_id}, Topic: {topic})"
    )


def handle_bulkadd(message):
    global NEXT_Q_ID

    chat_id = message["chat"]["id"]

    if not teacher_allowed(message):
        send_msg(chat_id, "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "") or ""
    lines = text.splitlines()

    if len(lines) <= 1:
        send_msg(
            chat_id,
            "Usage:\n"
            "/bulkadd\n"
            "/addq Topic | प्रश्न | A | B | C | D | सही(1-4) | व्याख्या\n"
            "/addq Topic | ...\n"
            "/addq ..."
        )
        return

    added = 0
    errors = []

    for lineno, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("/addq"):
            line = line[len("/addq"):].strip()

        parts = [p.strip() for p in line.split("|")]

        if len(parts) < 7:
            errors.append(f"Line {lineno}: फॉर्मेट गलत है (कम से कम 7 हिस्से चाहिए)।")
            continue

        if len(parts) == 7:
            topic = "General"
            question = parts[0]
            options = parts[1:5]
            correct_str = parts[5]
            explanation = parts[6]
        else:
            topic = parts[0] or "General"
            question = parts[1]
            options = parts[2:6]
            correct_str = parts[6]
            explanation = parts[7]

        if len(options) != 4:
            errors.append(f"Line {lineno}: exactly 4 options (A,B,C,D) देने हैं।")
            continue

        try:
            correct_num = int(correct_str)
            if correct_num not in (1, 2, 3, 4):
                raise ValueError
        except ValueError:
            errors.append(
                f"Line {lineno}: सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए (मिला: {correct_str!r})."
            )
            continue

        entry = {
            "id": NEXT_Q_ID,
            "topic": topic,
            "question": question,
            "options": options,
            "correct": correct_num - 1,
            "explanation": explanation,
        }
        QUESTIONS.append(entry)
        NEXT_Q_ID += 1
        added += 1

    save_questions_to_file()

    msg = f"✅ {added} सवाल bulk में जोड़ दिए गए हैं."
    if errors:
        msg += "\n\n⚠️ कुछ lines में error थी:\n" + "\n".join(errors[:5])
        if len(errors) > 5:
            msg += f"\n(+ {len(errors)-5} और lines में error...)"

    send_msg(chat_id, msg)


def handle_removeq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "") or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        send_msg(
            message["chat"]["id"],
            "Usage:\n"
            "/removeq <ID>\n"
            "या multiple IDs:\n"
            "/removeq 5 7 9\n"
            "/removeq 3,4,10"
        )
        return

    ids_part = parts[1]
    raw_tokens = ids_part.replace(",", " ").split()
    if not raw_tokens:
        send_msg(message["chat"]["id"], "कृपया कम से कम एक ID दें।")
        return

    removed_ids = []
    not_found_ids = []
    invalid_tokens = []

    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue
        try:
            q_id = int(token)
        except ValueError:
            invalid_tokens.append(token)
            continue

        idx = find_question_index_by_id(q_id)
        if idx == -1:
            not_found_ids.append(q_id)
            continue

        QUESTIONS.pop(idx)
        removed_ids.append(q_id)

    if removed_ids:
        save_questions_to_file()

    msg_lines = []
    if removed_ids:
        removed_ids_str = ", ".join(str(x) for x in removed_ids)
        msg_lines.append(f"🗑 हटाए गए सवाल (IDs): {removed_ids_str}")
    else:
        msg_lines.append("कोई भी सवाल remove नहीं हुआ।")

    if not_found_ids:
        nf_str = ", ".join(str(x) for x in not_found_ids)
        msg_lines.append(f"❓ ये IDs नहीं मिलीं: {nf_str}")

    if invalid_tokens:
        inv_str = ", ".join(invalid_tokens)
        msg_lines.append(f"⚠️ ये valid ID नहीं थीं: {inv_str}")

    send_msg(message["chat"]["id"], "\n".join(msg_lines))


def handle_editq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "")
    content = text[len("/editq"):].strip()
    parts = [p.strip() for p in content.split("|")]

    if len(parts) < 8:
        send_msg(
            message["chat"]["id"],
            "फॉर्मेट गलत है.\nउदाहरण:\n"
            "/editq 5 | नया प्रश्न | Option A | Option B | Option C | Option D | 2 | नई व्याख्या\n"
            "(Topic वही रहेगा जो पहले था)"
        )
        return

    id_str = parts[0]
    try:
        q_id = int(id_str)
    except ValueError:
        send_msg(message["chat"]["id"], "ID एक संख्या होनी चाहिए।")
        return

    idx = find_question_index_by_id(q_id)
    if idx == -1:
        send_msg(message["chat"]["id"], f"ID {q_id} वाला कोई सवाल नहीं मिला।")
        return

    question = parts[1]
    options = parts[2:6]
    correct_str = parts[6]
    explanation = parts[7]

    if len(options) != 4:
        send_msg(message["chat"]["id"], "आपको 4 options देने हैं (A, B, C, D).")
        return

    try:
        correct_num = int(correct_str)
        if correct_num not in (1, 2, 3, 4):
            raise ValueError
    except ValueError:
        send_msg(message["chat"]["id"], "सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए।")
        return

    q = QUESTIONS[idx]
    q["question"] = question
    q["options"] = options
    q["correct"] = correct_num - 1
    q["explanation"] = explanation

    save_questions_to_file()
    send_msg(
        message["chat"]["id"],
        f"✏️ सवाल update कर दिया गया है (ID: {q_id}, Topic: {q.get('topic','General')})."
    )


def handle_resetboard(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    chat_id = message["chat"]["id"]
    leaderboard.pop(chat_id, None)
    save_leaderboard_to_file()
    send_msg(chat_id, "✅ इस group का leaderboard reset कर दिया गया है।")


def handle_listq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    if not QUESTIONS:
        send_msg(message["chat"]["id"], "अभी कोई सवाल नहीं है।")
        return

    chat_id = message["chat"]["id"]
    lines = []
    count = 0

    for q in QUESTIONS:
        q_id = q.get("id")
        topic = q.get("topic", "General")
        text = q.get("question", "")
        preview = text.replace("\n", " ")
        if len(preview) > 60:
            preview = preview[:57] + "..."
        lines.append(f"{q_id}. [{topic}] {preview}")
        count += 1
        if count % 30 == 0:
            send_msg(chat_id, "\n".join(lines))
            lines = []

    if lines:
        send_msg(chat_id, "\n".join(lines))

    send_msg(chat_id, "ℹ️ पूरा questions bank देखने के लिए /exportq या /exportpdf चलाएँ।")


def handle_exportq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    if not QUESTIONS:
        send_msg(message["chat"]["id"], "अभी कोई सवाल नहीं है, export नहीं कर सकते।")
        return

    chat_id = message["chat"]["id"]
    export_path = os.path.join(BASE_DIR, "questions_export.txt")

    lines = []
    for q in QUESTIONS:
        q_id = q.get("id")
        lines.append(f"ID: {q_id}")
        lines.append(f"Topic: {q.get('topic','General')}")
        lines.append(f"Question: {q.get('question','')}")
        opts = q.get("options", [])
        for idx, opt in enumerate(opts, start=1):
            lines.append(f"  {idx}. {opt}")
        correct_idx = q.get("correct", 0)
        if 0 <= correct_idx < len(opts):
            lines.append(f"Correct: {correct_idx+1} ({opts[correct_idx]})")
        else:
            lines.append("Correct: (invalid index)")
        lines.append(f"Explanation: {q.get('explanation','')}")
        lines.append("-" * 40)

    try:
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        log.error("export file लिखने में error: %s", e)
        send_msg(chat_id, "❌ export file नहीं बना पाए।")
        return

    res = send_document(chat_id, export_path, caption="📄 BPSC IntelliQuiz - Questions Export (TXT)")
    if not res or not res.get("ok"):
        send_msg(chat_id, "❌ export TXT file भेजने में समस्या आई।")
    else:
        send_msg(chat_id, "✅ Questions bank TXT के रूप में export कर दिया गया है।")


# ---------------- PDF EXPORT HELPERS ----------------


def create_questions_pdf(pdf_path, topic_questions=None, topic_label=None):
    font_name = "Helvetica"
    try:
        if os.path.exists(PDF_FONT_PATH):
            pdfmetrics.registerFont(TTFont("Devanagari", PDF_FONT_PATH))
            font_name = "Devanagari"
    except Exception as e:
        log.error("PDF font register error: %s", e)

    c = canvas.Canvas(pdf_path, pagesize=A4)
    width, height = A4
    c.setFont(font_name, 11)

    left_margin = 40
    top_margin = height - 40
    line_height = 14
    y = top_margin

    def draw_line(text):
        nonlocal y
        max_chars = 95
        text = text.replace("\\r", "").replace("\\n", " ")
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        for ch in chunks:
            if y <= 40:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin
            c.drawString(left_margin, y, ch)
            y -= line_height

    q_source = topic_questions if topic_questions is not None else QUESTIONS

    header = "BPSC IntelliQuiz - Questions Export"
    if topic_label:
        header += f" - Topic: {topic_label}"
    draw_line(header)
    draw_line("=" * 60)
    for q in q_source:
        q_id = q.get("id")
        topic = q.get("topic", "General")
        question = q.get("question", "")
        opts = q.get("options", [])
        correct_idx = q.get("correct", 0)
        explanation = q.get("explanation", "")

        draw_line(f"ID: {q_id}  |  Topic: {topic}")
        draw_line(f"Q: {question}")
        for idx, opt in enumerate(opts, start=1):
            draw_line(f"  {idx}. {opt}")
        if 0 <= correct_idx < len(opts):
            draw_line(f"Correct: {correct_idx+1} ({opts[correct_idx]})")
        else:
            draw_line("Correct: (invalid index)")
        draw_line(f"Explanation: {explanation}")
        draw_line("-" * 40)

    c.save()



def handle_exportpdf(message):
    # Support: "/exportpdf" or "/exportpdf TopicName"
    try:
        text = message.get("text", "") or ""
        parts = text.split(maxsplit=1)
        topic_arg = None
        if len(parts) > 1:
            topic_arg = parts[1].strip()

        if not QUESTIONS:
            send_msg(message["chat"]["id"], "अभी कोई सवाल नहीं है, PDF export नहीं कर सकते।")
            return

        # filter questions
        if topic_arg:
            topic_questions = [q for q in QUESTIONS if str(q.get("topic","")).strip().lower() == topic_arg.lower()]
            if not topic_questions:
                send_msg(message["chat"]["id"], f"No questions found for topic '{topic_arg}'.")
                return
            pdf_path = os.path.join(BASE_DIR, f"questions_export_{topic_arg.replace(' ','_')}.pdf")
            create_questions_pdf(pdf_path, topic_questions=topic_questions, topic_label=topic_arg)
            send_document(message["chat"]["id"], pdf_path, caption=f"📄 Questions Export - {topic_arg}")
            return

        # default: export all
        pdf_path = os.path.join(BASE_DIR, "questions_export.pdf")
        create_questions_pdf(pdf_path)
        send_document(message["chat"]["id"], pdf_path, caption="📄 Questions Export")
    except Exception as e:
        log.error("exportpdf error: %s", e)
        send_msg(message["chat"]["id"], "PDF export करते समय error आया।")

def handle_settime(message):
    global QUESTION_TIME

    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    parts = message.get("text", "").split()
    if len(parts) < 2:
        send_msg(
            message["chat"]["id"],
            "Usage: /settime <seconds>\nउदाहरण: /settime 60  (मतलब 60 सेकंड प्रति सवाल)"
        )
        return

    try:
        sec = int(parts[1])
    except ValueError:
        send_msg(message["chat"]["id"], "समय एक संख्या होना चाहिए (seconds में)।")
        return

    if not 5 <= sec <= 600:
        send_msg(message["chat"]["id"], "समय 5 से 600 सेकंड के बीच होना चाहिए।")
        return

    QUESTION_TIME = sec
    save_settings()
    send_msg(
        message["chat"]["id"],
        f"✅ सवाल का समय अब *{QUESTION_TIME} सेकंड* कर दिया गया है।",
        parse_mode="Markdown",
    )



# ---------------- PRIVATE /test <Topic> (per-user) ----------------
private_tests = {}  # user_id -> {"questions": [...], "index": 0, "score": 0}

def handle_test(message):
    chat = message["chat"]
    chat_id = chat["id"]
    if chat.get("type") != "private":
        send_msg(chat_id, "कृपया यह command private chat में चलाएँ.\nUse: /test TopicName (in bot's private chat)")
        return

    text = message.get("text", "") or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        send_msg(chat_id, "Usage: /test TopicName\nExample: /test Balwant")
        return
    topic_arg = parts[1].strip()

    topic_questions = [q for q in QUESTIONS if str(q.get("topic","")).strip().lower() == topic_arg.lower()]
    if not topic_questions:
        send_msg(chat_id, f"No questions found for topic '{topic_arg}'.")
        return

    # prepare a shallow copy with correct indices adjusted to 1-based for user
    qlist = []
    for q in topic_questions:
        qlist.append(q)

    private_tests[chat_id] = {
        "topic": topic_arg,
        "questions": qlist,
        "index": 0,
        "score": 0
    }
    send_msg(chat_id, f"Starting private test for topic: {topic_arg}\nTotal Q: {len(qlist)}\nReply with 1/2/3/4 for choices.")
    ask_private_question(chat_id)

def ask_private_question(user_id):
    st = private_tests.get(user_id)
    if not st:
        return
    idx = st["index"]
    qlist = st["questions"]
    if idx >= len(qlist):
        send_msg(user_id, f"Test finished for topic '{st['topic']}'. Score: {st['score']}/{len(qlist)}")
        private_tests.pop(user_id, None)
        return
    q = qlist[idx]
    text = f"Q{idx+1}. {q['question']}\n\n1. {q['options'][0]}\n2. {q['options'][1]}\n3. {q['options'][2]}\n4. {q['options'][3]}"
    send_msg(user_id, text)

def check_private_answer(message):
    user_id = message["from"]["id"]
    chat = message["chat"]
    if chat.get("type") != "private":
        return  # ignore non-private replies for private tests
    st = private_tests.get(user_id)
    if not st:
        return  # no active test
    text = message.get("text", "").strip()
    try:
        ans = int(text)
    except Exception:
        send_msg(user_id, "Please reply with choice number 1-4 only.")
        return
    if ans < 1 or ans > 4:
        send_msg(user_id, "Choice must be 1-4.")
        return
    q = st["questions"][st["index"]]
    correct = q.get("correct", 0) + 1  # stored as 0-based
    if ans == correct:
        st["score"] += 1
        send_msg(user_id, "Correct ✅")
    else:
        send_msg(user_id, f"Wrong ❌\nCorrect: {correct}. Explanation: {q.get('explanation','-')}")
    st["index"] += 1
    ask_private_question(user_id)


# ---------------- MAIN LOOP (Render-friendly) ----------------
def main():
    log.info("🔁 Bot started polling (Render-ready long polling)...")
    offset = None

    while True:
        try:
            timeout_check()

            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset

            updates = api_call("getUpdates", params)
            if not updates or not updates.get("ok"):
                time.sleep(1)
                continue

            for upd in updates["result"]:
                offset = upd["update_id"] + 1

                if "message" in upd:
                    msg = upd["message"]
                    text = msg.get("text", "") or ""

                    if text.startswith("/start"):
                        start_command(msg)
                    elif text.startswith("/quiz"):
                        start_quiz(msg)
                    elif text.startswith("/quiz_pause"):
                        quiz_pause(msg)
                    elif text.startswith("/quiz_resume"):
                        quiz_resume(msg)
                    elif text.startswith("/quiz_stop"):
                        quiz_stop(msg)
                    elif text.startswith("/leaderboard_today"):
                        handle_leaderboard_today(msg)
                    elif text.startswith("/leaderboard_week"):
                        handle_leaderboard_week(msg)
                    elif text.startswith("/leaderboard_month"):
                        handle_leaderboard_month(msg)
                    elif text.startswith("/leaderboard"):
                        show_leaderboard(msg)
                    elif text.startswith("/addq"):
                        handle_addq(msg)
                    elif text.startswith("/bulkadd"):
                        handle_bulkadd(msg)
                    elif text.startswith("/editq"):
                        handle_editq(msg)
                    elif text.startswith("/removeq"):
                        handle_removeq(msg)
                    elif text.startswith("/resetboard"):
                        handle_resetboard(msg)
                    elif text.startswith("/listq"):
                        handle_listq(msg)
                    elif text.startswith("/exportq"):
                        handle_exportq(msg)
                    elif text.startswith("/exportpdf"):
                        handle_exportpdf(msg)
                    elif text.startswith("/test"):
                        handle_test(msg)
                    elif text.startswith("/settime"):
                        handle_settime(msg)

                if "callback_query" in upd:
                    handle_answer(upd["callback_query"])

        except KeyboardInterrupt:
            log.info("⛔ KeyboardInterrupt मिला, bot बंद कर रहे हैं।")
            break
        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(2)


# ---------------- RUN BOT ----------------
if __name__ == "__main__":
    log.info("🚀 BPSC IntelliQuiz Bot starting up...")
    load_settings()
    load_questions_from_db()        # Supabase से load
    load_leaderboard_from_file()
    load_results_history_from_file()
    main()



# --- Appended DB-backed handlers ---

@bot.message_handler(commands=['listtopics'])
def handle_listtopics(message):
    topics = db_get_topics()
    if not topics:
        bot.reply_to(message, "कोई topic नहीं मिला. पहले कुछ प्रश्न /addq से डालें.")
        return
    text = "Available Topics:\n" + "\n".join(f"- {t}" for t in topics)
    bot.reply_to(message, text)



@bot.message_handler(commands=['test'])
def start_topic_test(message):
    topic = message.text.replace("/test", "", 1).strip()
    if not topic:
        bot.reply_to(message, "Use: /test TopicName")
        return

    # fetch from DB
    topic_questions = db_get_questions_by_topic(topic)
    if not topic_questions:
        bot.reply_to(message, f"No questions found for topic '{topic}'.")
        return

    USER_STATE[message.chat.id] = {
        "topic": topic,
        "questions": topic_questions,
        "index": 0,
        "score": 0
    }
    ask_question(message.chat.id)

