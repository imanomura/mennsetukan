import os
import sqlite3
import time
import uuid
import socket
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "koto_sync.sqlite3"
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ==========================================
# リアルタイム通信（UDP）の設定（Step 2用）
# ==========================================
ENABLE_UDP_NOTIFY = os.environ.get("ENABLE_UDP_NOTIFY", "False").lower() in ("true", "1")
VSCODE_PC_IP = os.environ.get("VSCODE_PC_IP", "127.0.0.1")
VSCODE_UDP_PORT = int(os.environ.get("VSCODE_UDP_PORT", "5005"))

def send_udp_notification(event_type, question_id):
    if not ENABLE_UDP_NOTIFY:
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        message = f"EVENT:{event_type},Q_ID:{question_id}".encode('utf-8')
        sock.sendto(message, (VSCODE_PC_IP, VSCODE_UDP_PORT))
        sock.close()
    except Exception as e:
        print(f"UDP送信エラー: {e}")

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
            client_time TEXT, video_time_sec REAL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            question_id INTEGER, event_type TEXT, action TEXT NOT NULL,
            operation_epoch REAL NOT NULL, operation_time TEXT NOT NULL,
            event_id INTEGER, undone INTEGER NOT NULL DEFAULT 0,
            previous_epoch REAL, previous_time TEXT, previous_client_time TEXT
        )""")
        
        # 既存DBへのカラム追加互換性
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "video_time_sec" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN video_time_sec REAL")

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

    allowed_event_types = {
        "question_end", "interviewer_question_end", "participant_answer_end"
    }
    event_type = str(payload.get("event_type", "interviewer_question_end"))
    if not session_id or not 1 <= question_id <= 9 or event_type not in allowed_event_types:
        return jsonify(error="セッションID、質問番号、イベント種別を確認してください。"), 400

    # 手動で指定された刻印時刻、または動画内秒数があれば反映
    manual_time = payload.get("manual_time")
    video_time_sec = payload.get("video_time_sec")
    
    if manual_time:
        try:
            dt = datetime.fromisoformat(str(manual_time).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            server_epoch = dt.timestamp()
            server_time = dt.isoformat(timespec="milliseconds")
        except ValueError:
            return jsonify(error="指定された修正時刻のフォーマットが不正です。"), 400
    else:
        server_epoch = time.time()
        server_time = utc_now()

    client_time = str(payload.get("client_time", ""))[:80]

    with db() as conn:
        exists = conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not exists:
            return jsonify(error="セッションがありません。先にセッションを作成してください。"), 404

        matching_types = ("interviewer_question_end", "question_end") if event_type in (
            "interviewer_question_end", "question_end"
        ) else (event_type,)
        placeholders = ",".join("?" for _ in matching_types)
        
        # 既存データを無条件削除して置換
        old_ids = [row["id"] for row in conn.execute(
            f"SELECT id FROM events WHERE session_id=? AND question_id=? AND event_type IN ({placeholders})",
            (session_id, question_id, *matching_types)
        ).fetchall()]
        
        if old_ids:
            id_placeholders = ",".join("?" for _ in old_ids)
            conn.execute(f"DELETE FROM operation_logs WHERE event_id IN ({id_placeholders})", old_ids)
            conn.execute(f"DELETE FROM events WHERE id IN ({id_placeholders})", old_ids)

        cursor = conn.execute(
            """INSERT INTO events(session_id, question_id, event_type, server_epoch, server_time, client_time, video_time_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, question_id, event_type, server_epoch, server_time, client_time or None, video_time_sec),
        )
        event_id = cursor.lastrowid
        record_operation(conn, session_id, "replace" if old_ids else "create", question_id, event_type, event_id)

    send_udp_notification(event_type, question_id)
    return jsonify(ok=True, id=event_id, session_id=session_id, question_id=question_id,
                   event_type=event_type, server_epoch=server_epoch, server_time=server_time, video_time_sec=video_time_sec)

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
        conn.execute("DELETE FROM events WHERE id=? AND session_id=?", (latest["event_id"], session_id))
        conn.execute("UPDATE operation_logs SET undone=1 WHERE id=?", (latest["id"],))
        record_operation(conn, session_id, "undo", latest["question_id"], latest["event_type"], latest["event_id"])
    return jsonify(ok=True, session_id=session_id, question_id=latest["question_id"])

@app.get("/api/events")
def get_events():
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify(error="session_idが必要です。"), 400
    with db() as conn:
        rows = conn.execute(
            "SELECT id, session_id, question_id, event_type, server_epoch, server_time, client_time, video_time_sec "
            "FROM events WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall()
    return jsonify(events=[dict(row) for row in rows])

@app.get("/api/sessions/<session_id>/export.csv")
def export_events(session_id):
    import csv
    from io import StringIO
    with db() as conn:
        rows = conn.execute(
            "SELECT id, session_id, question_id, event_type, server_epoch, server_time, client_time, video_time_sec "
            "FROM events WHERE session_id=? ORDER BY question_id, id", (session_id,)
        ).fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["event_id", "session_id", "question_id", "event_type", "server_epoch", "server_epoch_ms", "server_time_utc", "client_time", "video_time_sec"])
    for row in rows:
        server_epoch = row["server_epoch"]
        server_epoch_ms = int(server_epoch * 1000)
        writer.writerow([
            row["id"], row["session_id"], row["question_id"], row["event_type"],
            server_epoch, server_epoch_ms, row["server_time"], row["client_time"] or "", row["video_time_sec"] or ""
        ])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={session_id}_interview_events.csv"})
    from flask import send_from_directory

# 録画動画をブラウザへストリーミング配信するエンドポイント
@app.route('/api/video/stream')
def stream_video():
    filepath = request.args.get('path', '').strip()
    if not filepath or not os.path.exists(filepath):
        return jsonify(error="指定された動画ファイルが見つかりません。"), 404
    
    dirname = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    return send_from_directory(dirname, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)