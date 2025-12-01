import time
import requests
import logging
import random

# ---------------- CONFIG ----------------
BOT_TOKEN = "7688080597:AAGdZu38mxpqbBH3fWx_c3hspdPwjiiZKug"   # <-- यहाँ अपना BotFather वाला token डालो
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

QUESTION_TIME = 45   # हर सवाल के लिए समय (seconds)
POLL_TIMEOUT = 20    # getUpdates long polling timeout

# Negative marking rules
MARK_CORRECT = 1.0       # सही उत्तर पर इतना + मिलेगा
MARK_WRONG = -0.33       # गलत उत्तर पर इतना - कटेगा


# ---------------- QUESTIONS (GLOBAL BANK) ----------------
# NOTE: /addq और /bulkadd से यहीं new questions memory में add होंगे
QUESTIONS = [
    {
        "question": "1. मौर्य साम्राज्य की स्थापना किसने की?",
        "options": ["A) बिन्दुसार", "B) चंद्रगुप्त मौर्य", "C) अशोक", "D) पुष्यमित्र शुंग"],
        "correct": 1,  # 0-based index (0=A, 1=B, ...)
        "explanation": "चंद्रगुप्त मौर्य ने 322 ई.पू. में मौर्य साम्राज्य की स्थापना की।"
    },
    
]

# ---------------- GLOBAL STATE ----------------
# group_state[chat_id] = {
#   "order": [question_index_list],
#   "q_index": current_index_in_order,
#   "start": question_start_time,
#   "answers": {user_id: True}  # इस सवाल के लिए किसने answer दे दिया
#   "user_stats": {user_id: {"correct": int, "wrong": int, "attempted": int}},
# }
group_state = {}

# leaderboard[chat_id][user_id] = {"name": str, "score": float}
leaderboard = {}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BOT")


# ---------------- BASIC TELEGRAM FUNCTIONS ----------------
def api_call(method, params=None):
    try:
        r = requests.get(f"{API_URL}/{method}", params=params, timeout=POLL_TIMEOUT + 5)
        return r.json()
    except Exception as e:
        log.error("API error: %s", e)
        return None


def send_msg(chat_id, text, reply_markup=None, parse_mode=None):
    import json
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        params["parse_mode"] = parse_mode
    return api_call("sendMessage", params)


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

    # private chat में सबको allow
    if chat_type == "private":
        return True

    member = get_chat_member(message["chat"]["id"], user["id"])
    return member and member["status"] in ("administrator", "creator")


def teacher_allowed(message):
    chat_type = message["chat"]["type"]
    if chat_type == "private":
        return True
    return is_admin(message)


# ---------------- BASIC COMMANDS ----------------
def start_command(message):
    chat_id = message["chat"]["id"]
    text = (
        "नमस्ते! 👋\n"
        "मैं Mauryan Quiz Bot हूँ.\n\n"
        "🔹 Student commands:\n"
        "• /quiz short – 5 सवाल का छोटा क्विज़\n"
        "• /quiz full – बड़ा टेस्ट (max 25 सवाल)\n"
        "• /leaderboard – इस group का स्कोर\n\n"
        "🔹 Teacher/Admin commands:\n"
        "• /addq प्रश्न | A | B | C | D | सही (1-4) | व्याख्या\n"
        "• /bulkadd + कई /addq lines – एक साथ कई सवाल जोड़ें\n"
        "• /removeq <id> – सवाल हटाएँ\n"
        "• /resetboard – leaderboard साफ़ करें\n"
        "• /listq – सभी questions की list\n"
    )
    send_msg(chat_id, text)


def parse_quiz_mode(text: str) -> str:
    parts = text.split()
    if len(parts) > 1:
        mode = parts[1].lower()
        if mode in ("short", "full"):
            return mode
    # default
    return "short"


# ---------------- QUIZ START / FLOW ----------------
def start_quiz(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "") or ""

    if not is_admin(message):
        send_msg(chat_id, "केवल admin /quiz चला सकता है।")
        return

    mode = parse_quiz_mode(text)
    total_available = len(QUESTIONS)
    if total_available == 0:
        send_msg(chat_id, "अभी कोई सवाल मौजूद नहीं है। पहले /addq या /bulkadd से सवाल जोड़ें।")
        return

    if mode == "full":
        desired = 25
    else:
        desired = 5

    count = min(desired, total_available)

    # random order बनाओ
    order = list(range(total_available))
    random.shuffle(order)
    order = order[:count]

    group_state[chat_id] = {
        "order": order,
        "q_index": 0,
        "start": time.time(),
        "answers": {},
        "user_stats": {},
    }

    send_msg(
        chat_id,
        f"🎯 Quiz शुरू!\nMode: {mode} | Questions: {count}\n"
        f"हर सवाल का समय: {QUESTION_TIME} सेकंड\n"
        f"Marking: सही = {MARK_CORRECT}, गलत = {MARK_WRONG}\n"
        "आपका detailed result private chat में आएगा।"
    )

    send_question(chat_id)


def send_question(chat_id):
    st = group_state.get(chat_id)
    if not st:
        return

    order = st["order"]
    q_idx = st["q_index"]

    if q_idx >= len(order):
        return

    q = QUESTIONS[order[q_idx]]

    # inline keyboard
    buttons = [
        [{"text": opt, "callback_data": f"ans_{i}"}]
        for i, opt in enumerate(q["options"])
    ]
    markup = {"inline_keyboard": buttons}

    text = f"📝 सवाल {q_idx+1}/{len(order)} (⏱ {QUESTION_TIME} सेकंड)\n\n{q['question']}"
    send_msg(chat_id, text, reply_markup=markup)

    st["start"] = time.time()
    st["answers"] = {}   # नए सवाल के लिए reset


def timeout_check():
    """
    हर loop में चलाकर check करते हैं किस group का question time खत्म हो चुका है।
    """
    now = time.time()
    for chat_id, st in list(group_state.items()):
        if now - st.get("start", now) >= QUESTION_TIME:
            finish_question(chat_id)


def finish_question(chat_id):
    st = group_state.get(chat_id)
    if not st:
        return

    order = st["order"]
    q_idx = st["q_index"]

    if q_idx >= len(order):
        return

    q = QUESTIONS[order[q_idx]]
    correct = q["correct"]

    # Group में सही उत्तर + explanation
    summary = (
        "⏰ समय समाप्त!\n"
        f"✅ सही उत्तर: {q['options'][correct]}\n\n"
        f"ℹ️ व्याख्या:\n{q['explanation']}"
    )
    send_msg(chat_id, summary)

    # अगले सवाल पर जाएँ
    st["q_index"] += 1

    if st["q_index"] < len(order):
        # अभी और सवाल बचे हैं
        send_question(chat_id)
    else:
        # Quiz खत्म
        send_msg(chat_id, "🎉 Quiz खत्म! नीचे Leaderboard और आपकी summary भेजी जा रही है…")

        # सबको DM में summary
        send_user_summaries(chat_id)

        # Group में leaderboard
        send_leaderboard(chat_id)


# ---------------- ANSWER HANDLING ----------------
def handle_answer(cb):
    user = cb["from"]
    user_id = user["id"]
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data", "")
    cb_id = cb["id"]

    st = group_state.get(chat_id)
    if not st:
        answer_callback(cb_id, "अभी कोई quiz active नहीं है।")
        return

    # टाइम खत्म हो गया?
    if time.time() - st.get("start", 0) > QUESTION_TIME:
        answer_callback(cb_id, "इस सवाल का समय समाप्त हो चुका है।")
        return

    # क्या पहले से जवाब दे चुका है?
    if user_id in st["answers"]:
        answer_callback(cb_id, "आप पहले ही इस सवाल का जवाब दे चुके हैं।")
        return

    # चुना गया option
    try:
        selected = int(data.split("_")[1])
    except Exception:
        answer_callback(cb_id, "Invalid answer.")
        return

    order = st["order"]
    q_idx = st["q_index"]
    if q_idx >= len(order):
        answer_callback(cb_id, "Quiz समाप्त हो चुका है।")
        return

    q = QUESTIONS[order[q_idx]]
    correct = q["correct"]
    is_right = (selected == correct)

    # -------- User-wise stats (summary के लिए) ----------
    stats = st.setdefault("user_stats", {})
    u_stats = stats.get(user_id, {"correct": 0, "wrong": 0, "attempted": 0})
    u_stats["attempted"] += 1
    if is_right:
        u_stats["correct"] += 1
    else:
        u_stats["wrong"] += 1
    stats[user_id] = u_stats

    # -------- Leaderboard update (negative marking सहित) ----------
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

    # mark कि इस सवाल पर जवाब दे चुका है
    st["answers"][user_id] = True

    # Private feedback
    status_text = "✔ सही" if is_right else "❌ गलत"
    dm_text = (
        f"सवाल: {q['question']}\n"
        f"आपका जवाब: {q['options'][selected]}\n"
        f"{status_text}\n\n"
        f"ℹ️ व्याख्या:\n{q['explanation']}"
    )
    send_msg(user_id, dm_text)

    answer_callback(cb_id, "जवाब दर्ज किया गया!")


# ---------------- SUMMARY + LEADERBOARD ----------------
def send_user_summaries(chat_id):
    st = group_state.get(chat_id)
    if not st:
        return

    stats = st.get("user_stats", {})
    board = leaderboard.get(chat_id, {})
    total_q = len(st["order"])

    for user_id, u_stats in stats.items():
        correct = u_stats.get("correct", 0)
        wrong = u_stats.get("wrong", 0)
        attempted = u_stats.get("attempted", 0)
        skipped = total_q - attempted

        score = 0.0
        if user_id in board:
            score = board[user_id].get("score", 0.0)

        summary_text = (
            "📊 आपका Quiz Summary:\n\n"
            f"कुल प्रश्न: {total_q}\n"
            f"सही: {correct}\n"
            f"गलत: {wrong}\n"
            f"नहीं किए: {skipped}\n\n"
            f"Final Score (नेगेटिव मार्किंग सहित): {score:.2f}\n"
        )

        send_msg(user_id, summary_text)


def send_leaderboard(chat_id):
    board = leaderboard.get(chat_id, {})
    if not board:
        send_msg(chat_id, "अभी कोई स्कोर नहीं है।")
        return

    sorted_board = sorted(board.items(), key=lambda x: x[1]["score"], reverse=True)

    text = "🏆 *Leaderboard* (नेगेटिव मार्किंग सहित)\n\n"
    for rank, (uid, data) in enumerate(sorted_board, 1):
        text += f"{rank}. {data['name']} — {data['score']:.2f}\n"

    send_msg(chat_id, text, parse_mode="Markdown")


def show_leaderboard(message):
    chat_id = message["chat"]["id"]
    send_leaderboard(chat_id)


# ---------------- TEACHER COMMANDS ----------------
def handle_addq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "")
    content = text[len("/addq"):].strip()
    parts = [p.strip() for p in content.split("|")]

    if len(parts) < 7:
        send_msg(
            message["chat"]["id"],
            "फॉर्मेट गलत है.\nउदाहरण:\n"
            "/addq प्रश्न | Option A | Option B | Option C | Option D | 2 | व्याख्या"
        )
        return

    question = parts[0]
    options = parts[1:5]
    correct_str = parts[5]
    explanation = parts[6]

    try:
        correct_num = int(correct_str)
    except ValueError:
        send_msg(message["chat"]["id"], "सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए।")
        return

    if not 1 <= correct_num <= 4:
        send_msg(message["chat"]["id"], "सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए।")
        return

    entry = {
        "question": question,
        "options": options,
        "correct": correct_num - 1,  # 0-based
        "explanation": explanation,
    }

    QUESTIONS.append(entry)
    q_id = len(QUESTIONS)
    send_msg(message["chat"]["id"], f"✅ नया सवाल जोड़ दिया गया है। (ID: {q_id})")


def handle_bulkadd(message):
    """
    Format (एक ही message में):

    /bulkadd
    /addq प्रश्न | विकल्प A | विकल्प B | विकल्प C | विकल्प D | सही (1-4) | व्याख्या

    (ऐसी कई /addq lines नीचे लिख सकते हैं, बीच में खाली लाइन भी चलेगी)
    """
    chat_id = message["chat"]["id"]

    if not teacher_allowed(message):
        send_msg(chat_id, "आपको यह command चलाने की अनुमति नहीं है।")
        return

    text = message.get("text", "") or ""
    lines = text.splitlines()

    # सिर्फ /bulkadd अकेला भेज दिया हो तो
    if len(lines) <= 1:
        send_msg(
            chat_id,
            "Usage:\n"
            "/bulkadd\n"
            "/addq प्रश्न | A | B | C | D | सही(1-4) | व्याख्या\n"
            "/addq ...\n"
            "/addq ..."
        )
        return

    added = 0
    errors = []

    # पहली line /bulkadd है, इसलिए दूसरी line से शुरू
    for lineno, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if not line:
            continue  # खाली line skip

        # line अगर /addq से शुरू है तो prefix हटा दो
        if line.startswith("/addq"):
            line = line[len("/addq"):].strip()

        parts = [p.strip() for p in line.split("|")]

        if len(parts) < 7:
            errors.append(f"Line {lineno}: फॉर्मेट गलत है (7 हिस्से चाहिए)।")
            continue

        question = parts[0]
        options = parts[1:5]
        correct_str = parts[5]
        explanation = parts[6]

        try:
            correct_num = int(correct_str)
            if correct_num not in (1, 2, 3, 4):
                raise ValueError
        except ValueError:
            errors.append(f"Line {lineno}: सही विकल्प संख्या 1 से 4 के बीच होनी चाहिए (मिला: {correct_str!r}).")
            continue

        entry = {
            "question": question,
            "options": options,
            "correct": correct_num - 1,  # 0-based
            "explanation": explanation,
        }
        QUESTIONS.append(entry)
        added += 1

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

    parts = message.get("text", "").split()
    if len(parts) < 2:
        send_msg(message["chat"]["id"], "Usage: /removeq <id>")
        return

    try:
        q_id = int(parts[1])
    except ValueError:
        send_msg(message["chat"]["id"], "ID एक संख्या होनी चाहिए।")
        return

    idx = q_id - 1
    if not 0 <= idx < len(QUESTIONS):
        send_msg(message["chat"]["id"], "ऐसा कोई सवाल नहीं मिला।")
        return

    removed_q = QUESTIONS.pop(idx)
    send_msg(message["chat"]["id"], f"🗑 सवाल हटाया गया:\n{removed_q['question']}")


def handle_resetboard(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    chat_id = message["chat"]["id"]
    leaderboard.pop(chat_id, None)
    send_msg(chat_id, "✅ इस group का leaderboard reset कर दिया गया है।")


def handle_listq(message):
    if not teacher_allowed(message):
        send_msg(message["chat"]["id"], "आपको यह command चलाने की अनुमति नहीं है।")
        return

    if not QUESTIONS:
        send_msg(message["chat"]["id"], "अभी कोई सवाल नहीं है।")
        return

    lines = []
    for i, q in enumerate(QUESTIONS, start=1):
        lines.append(f"{i}. {q['question']}")
        if i % 20 == 0:
            send_msg(message["chat"]["id"], "\n".join(lines))
            lines = []
    if lines:
        send_msg(message["chat"]["id"], "\n".join(lines))


# ---------------- MAIN LOOP ----------------
def main():
    log.info("Bot started polling...")
    offset = None

    while True:
        # हर बार timer check करो
        timeout_check()

        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset

        updates = api_call("getUpdates", params)
        if not updates or not updates.get("ok"):
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
                elif text.startswith("/leaderboard"):
                    show_leaderboard(msg)
                elif text.startswith("/addq"):
                    handle_addq(msg)
                elif text.startswith("/bulkadd"):
                    handle_bulkadd(msg)
                elif text.startswith("/removeq"):
                    handle_removeq(msg)
                elif text.startswith("/resetboard"):
                    handle_resetboard(msg)
                elif text.startswith("/listq"):
                    handle_listq(msg)

            if "callback_query" in upd:
                handle_answer(upd["callback_query"])


# ---------------- RUN BOT ----------------
if __name__ == "__main__":
    main()
