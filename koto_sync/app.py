import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "koto_sync.sqlite3"
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', notes TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            question_id INTEGER NOT NULL, event_type TEXT NOT NULL,
            server_epoch REAL NOT NULL, server_time TEXT NOT NULL,
            client_time TEXT, UNIQUE(session_id, question_id, event_type),
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            question_id INTEGER, event_type TEXT, action TEXT NOT NULL,
            operation_epoch REAL NOT NULL, operation_time TEXT NOT NULL,
            event_id INTEGER, undone INTEGER NOT NULL DEFAULT 0,
            previous_epoch REAL, previous_time TEXT, previous_client_time TEXT
        )""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(operation_logs)")}
        for name, column_type in (
            ("previous_epoch", "REAL"),
            ("previous_time", "TEXT"),
            ("previous_client_time", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE operation_logs ADD COLUMN {name} {column_type}")


def record_operation(
    conn, session_id, action, question_id=None, event_type=None, event_id=None,
    previous_epoch=None, previous_time=None, previous_client_time=None,
):
    operation_epoch = time.time()
    operation_time = utc_now()
    conn.execute(
        """INSERT INTO operation_logs(
               session_id, question_id, event_type, action, operation_epoch,
               operation_time, event_id, previous_epoch, previous_time, previous_client_time
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, question_id, event_type, action, operation_epoch, operation_time,
         event_id, previous_epoch, previous_time, previous_client_time),
    )
    return operation_epoch, operation_time


init_db()


@app.get("/")
def home():
    return render_template("interviewer.html")


@app.get("/interviewer")
def interviewer():
    return render_template("interviewer.html")


@app.get("/health")
def health():
    return jsonify(ok=True, server_time=utc_now(), server_epoch=time.time())


@app.post("/api/session")
def create_session():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip() or uuid.uuid4().hex[:12]
    if len(session_id) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in session_id):
        return jsonify(error="セッションIDは英数字、ハイフン、アンダースコアで指定してください。"), 400
    now = utc_now()
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO sessions(session_id, created_at) VALUES (?, ?)", (session_id, now))
    return jsonify(session_id=session_id, server_time=now, server_epoch=time.time())


@app.post("/api/events")
def record_event():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    try:
        question_id = int(payload.get("question_id"))
    except (TypeError, ValueError):
        return jsonify(error="質問番号が不正です。"), 400
    # question_end は旧版クライアントとの互換性のため受け付ける。
    allowed_event_types = {
        "question_end",  # 旧版: 面接官の質問終了
        "interviewer_question_end",  # 面接官が質問を言い終えた時刻
        "participant_answer_end",  # 被験者が回答を終えた時刻
    }
    event_type = str(payload.get("event_type", "interviewer_question_end"))
    if not session_id or not 1 <= question_id <= 9 or event_type not in allowed_event_types:
        return jsonify(error="セッションID、質問番号、イベント種別を確認してください。"), 400
    server_epoch = time.time()
    server_time = utc_now()
    client_time = str(payload.get("client_time", ""))[:80]
    replace_existing = payload.get("replace_existing") is True
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not exists:
            return jsonify(error="セッションがありません。先にセッションを作成してください。"), 404
        matching_types = ("interviewer_question_end", "question_end") if event_type in (
            "interviewer_question_end", "question_end"
        ) else (event_type,)
        placeholders = ",".join("?" for _ in matching_types)
        existing_rows = conn.execute(
            f"""SELECT id, event_type FROM events
                WHERE session_id=? AND question_id=? AND event_type IN ({placeholders})
                ORDER BY id DESC""",
            (session_id, question_id, *matching_types),
        ).fetchall()
        if existing_rows and not replace_existing:
            return jsonify(error="既存のデータがあります。", code="existing_event"), 409
        if existing_rows:
            # Remove all data tied to old event IDs before creating a fresh event.
            old_ids = [row["id"] for row in existing_rows]
            id_placeholders = ",".join("?" for _ in old_ids)
            conn.execute(
                f"DELETE FROM operation_logs WHERE event_id IN ({id_placeholders})", old_ids
            )
            conn.execute(f"DELETE FROM events WHERE id IN ({id_placeholders})", old_ids)
        conn.execute(
            """INSERT INTO events(session_id, question_id, event_type, server_epoch, server_time, client_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, question_id, event_type, server_epoch, server_time, client_time or None),
        )
        row = conn.execute(
            "SELECT id FROM events WHERE session_id=? AND question_id=? AND event_type=?",
            (session_id, question_id, event_type),
        ).fetchone()
        event_id = row["id"]
        record_operation(
            conn, session_id, "replace" if existing_rows else "create",
            question_id, event_type, event_id,
        )
    return jsonify(ok=True, id=event_id, session_id=session_id, question_id=question_id,
                   event_type=event_type, server_epoch=server_epoch, server_time=server_time)


@app.post("/api/session/reset")
def reset_session():
    """指定セッションのイベント時刻だけを消し、同じIDで最初から記録し直せるようにする。"""
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return jsonify(error="セッションIDが必要です。"), 400
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not exists:
            return jsonify(error="セッションがありません。"), 404
        cur = conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
        deleted_count = cur.rowcount
        conn.execute("DELETE FROM operation_logs WHERE session_id=?", (session_id,))
        record_operation(conn, session_id, "reset")
    return jsonify(ok=True, session_id=session_id, deleted_count=deleted_count)


@app.post("/api/events/undo")
def undo_last_event_operation():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return jsonify(error="セッションIDが必要です。"), 400
    with db() as conn:
        latest = conn.execute(
            "SELECT * FROM operation_logs WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not latest or latest["action"] not in ("create", "replace", "correct") or latest["undone"]:
            return jsonify(error="取り消せる直前の時刻操作がありません。"), 409
        if latest["event_id"] is None:
            return jsonify(error="取り消し対象の記録が見つかりません。"), 409
        if latest["action"] == "correct" and latest["previous_epoch"] is not None:
            conn.execute(
                """UPDATE events SET server_epoch=?, server_time=?, client_time=?
                   WHERE id=? AND session_id=?""",
                (latest["previous_epoch"], latest["previous_time"],
                 latest["previous_client_time"], latest["event_id"], session_id),
            )
        else:
            conn.execute("DELETE FROM events WHERE id=? AND session_id=?", (latest["event_id"], session_id))
        conn.execute("UPDATE operation_logs SET undone=1 WHERE id=?", (latest["id"],))
        record_operation(
            conn, session_id, "undo", latest["question_id"], latest["event_type"], latest["event_id"]
        )
    return jsonify(
        ok=True, session_id=session_id, question_id=latest["question_id"],
        restored_previous=latest["action"] == "correct",
    )


@app.post("/api/events/correct")
def correct_event_time():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    try:
        question_id = int(payload.get("question_id"))
        corrected = datetime.fromisoformat(str(payload.get("server_time", "")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return jsonify(error="質問番号または修正時刻が不正です。"), 400
    event_type = str(payload.get("event_type", ""))
    if not session_id or not 1 <= question_id <= 9 or event_type not in (
        "interviewer_question_end", "participant_answer_end", "question_end"
    ):
        return jsonify(error="セッションID、質問番号、イベント種別を確認してください。"), 400
    if corrected.tzinfo is None:
        corrected = corrected.replace(tzinfo=timezone.utc)
    corrected = corrected.astimezone(timezone.utc)
    corrected_epoch = corrected.timestamp()
    corrected_time = corrected.isoformat(timespec="milliseconds")
    matching_types = ("interviewer_question_end", "question_end") if event_type in (
        "interviewer_question_end", "question_end"
    ) else (event_type,)
    placeholders = ",".join("?" for _ in matching_types)
    with db() as conn:
        row = conn.execute(
            f"""SELECT id, event_type, server_epoch, server_time, client_time FROM events
                WHERE session_id=? AND question_id=? AND event_type IN ({placeholders})
                ORDER BY id DESC LIMIT 1""",
            (session_id, question_id, *matching_types),
        ).fetchone()
        if not row:
            return jsonify(error="修正する記録がありません。"), 404
        conn.execute(
            "UPDATE events SET server_epoch=?, server_time=? WHERE id=?",
            (corrected_epoch, corrected_time, row["id"]),
        )
        conn.execute("DELETE FROM operation_logs WHERE event_id=?", (row["id"],))
        record_operation(
            conn, session_id, "correct", question_id, row["event_type"], row["id"],
            row["server_epoch"], row["server_time"], row["client_time"],
        )
        updated = conn.execute(
            "SELECT id, session_id, question_id, event_type, server_epoch, server_time, client_time "
            "FROM events WHERE id=?", (row["id"],)
        ).fetchone()
    return jsonify(ok=True, event=dict(updated))


@app.get("/api/events")
def get_events():
    session_id = request.args.get("session_id", "").strip()
    try:
        after_id = max(0, int(request.args.get("after_id", "0")))
    except ValueError:
        after_id = 0
    if not session_id:
        return jsonify(error="session_idが必要です。"), 400
    with db() as conn:
        rows = conn.execute(
            "SELECT id, session_id, question_id, event_type, server_epoch, server_time, client_time "
            "FROM events WHERE session_id=? AND id>? ORDER BY id ASC", (session_id, after_id)
        ).fetchall()
    return jsonify(events=[dict(row) for row in rows], server_epoch=time.time(), server_time=utc_now())


@app.get("/api/sessions/<session_id>/export.csv")
def export_events(session_id):
    import csv
    from io import StringIO
    with db() as conn:
        rows = conn.execute(
            "SELECT id, session_id, question_id, event_type, server_epoch, server_time, client_time "
            "FROM events WHERE session_id=? ORDER BY question_id, id", (session_id,)
        ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["event_id", "session_id", "question_id", "event_type", "server_epoch", "server_time_utc", "client_time"])
    for row in rows:
        writer.writerow([row["id"], row["session_id"], row["question_id"], row["event_type"], row["server_epoch"], row["server_time"], row["client_time"] or ""])
    from flask import Response
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={session_id}_interview_events.csv"})


@app.get("/api/sessions/<session_id>/operations.csv")
def export_operations(session_id):
    import csv
    from io import StringIO
    from flask import Response

    with db() as conn:
        rows = conn.execute(
            """SELECT id, session_id, question_id, event_type, action,
                      operation_epoch, operation_time, event_id, undone
               FROM operation_logs WHERE session_id=? ORDER BY id""",
            (session_id,),
        ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "operation_id", "session_id", "question_id", "event_type", "action",
        "operation_epoch", "operation_time_utc", "event_id", "undone",
    ])
    for row in rows:
        writer.writerow([row["id"], row["session_id"], row["question_id"] or "",
                         row["event_type"] or "", row["action"], row["operation_epoch"],
                         row["operation_time"], row["event_id"] or "", row["undone"]])
    return Response(
        "\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={session_id}_operation_log.csv"},
    )


if __name__ == "__main__":
    # Separate computers require this app to be hosted on a server reachable by both PCs.
    # Use a production WSGI server + HTTPS for real experiments; do not expose Flask debug mode.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
