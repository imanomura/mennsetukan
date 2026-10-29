import sys
import os

# ターミナル出力の文字コードを UTF-8 に固定する設定
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import sqlite3
from pathlib import Path

db_path = Path(__file__).resolve().parent / "koto_sync.sqlite3"

if not db_path.exists():
    print("[WARNING] koto_sync.sqlite3 が見つかりません。")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("=== 保存されている全セッションID一覧 ===")
    sessions = cursor.execute("SELECT session_id, created_at FROM sessions").fetchall()
    if not sessions:
        print("セッションデータはありません。")
    else:
        for s in sessions:
            print(f"- セッションID: {s[0]} (作成日時: {s[1]})")

    print("\n=== 各セッションの記録イベント数 ===")
    counts = cursor.execute("SELECT session_id, COUNT(*) FROM events GROUP BY session_id").fetchall()
    if not counts:
        print("イベントデータはありません。")
    else:
        for c in counts:
            print(f"- {c[0]}: {c[1]} 件のイベント記録あり")

    conn.close()