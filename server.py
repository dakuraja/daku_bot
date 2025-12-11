# server.py
# Flask server for health-checks, favicon, and runtime info for BPSC IntelliQuiz bot.

import time
import os
import json
from flask import Flask, jsonify, Response

app = Flask(__name__)
START_TS = time.time()

# Try to import bot.py to read QUESTIONS, NEXT_Q_ID, group_state etc.
BOT_MODULE = "bot"
_bot = None
try:
    import importlib
    _bot = importlib.import_module(BOT_MODULE)
except Exception as e:
    print("Failed to import bot module:", e)
    _bot = None


@app.route("/")
def home():
    return "<pre>BPSC IntelliQuiz Bot Server is running.\nUse /health for status.</pre>"


@app.route("/health")
def health():
    """
    JSON health:
    {
      "status": "ok",
      "uptime": 45.2,
      "questions_loaded": 10,
      "next_q_id": 41,
      "active_groups": 0,
      "mode": "polling"
    }
    """
    now = time.time()
    resp = {
        "status": "ok",
        "service": "BPSC IntelliQuiz Bot",
        "uptime_seconds": round(now - START_TS, 2),
        "timestamp": int(now)
    }

    try:
        if _bot:
            QUESTIONS = getattr(_bot, "QUESTIONS", None)
            NEXT_Q_ID = getattr(_bot, "NEXT_Q_ID", None)
            group_state = getattr(_bot, "group_state", None)
            mode = getattr(_bot, "WEBHOOK_ENABLED", None) or "polling"

            resp["questions_loaded"] = len(QUESTIONS) if isinstance(QUESTIONS, list) else None
            resp["next_q_id"] = NEXT_Q_ID
            resp["active_groups"] = len(group_state) if isinstance(group_state, dict) else 0
            resp["mode"] = mode
    except Exception as e:
        resp["error"] = str(e)

    return jsonify(resp)


@app.route("/favicon.ico")
def favicon():
    # Tiny transparent PNG (1x1) to remove 404 log spam
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00"
        b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return Response(png, mimetype="image/png")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
