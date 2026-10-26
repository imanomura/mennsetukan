# KOTO CLIP: 面接時刻記録・同期用ミニサーバー

## 構成
- `app.py`: Flaskサーバー。セッション作成、時刻イベント保存、CSV出力
- `templates/interviewer.html`: 面接官用画面。面接官の質問終了と被験者の回答終了を別々に記録
- `sync_client.py`: 被験者側Pythonシステムからイベントを取得するクライアント

## 記録するイベント
各質問（1〜9）について、以下の2つの時刻をサーバー側で記録します。
- `interviewer_question_end`: 面接官が質問を言い終えた時刻
- `participant_answer_end`: 被験者が回答を終えた時刻

旧版との互換性のため、APIは旧イベント種別 `question_end` も受け付けます。CSVには `event_type` 列があり、どちらの時刻かを区別できます。

## ローカル起動（動作確認用）
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install flask requests
python app.py
```
ブラウザで `http://127.0.0.1:5000/interviewer` を開きます。同じPCでの動作確認用です。

## 面接官側の操作
1. セッションID（例：`P001`。氏名・学籍番号は使用しない）を入力し、セッションを開始します。
2. 被験者側システムにも同じIDを設定します。
3. 面接官が質問を言い終えた直後に「面接官の質問終了を記録」を押します。
4. 被験者が回答を終えた直後に、同じ質問欄の「被験者が回答を終えた時刻を記録」を押します。
5. 画面を下へスクロールし、質問1から質問9まで順番に記録します。質問を選び直す必要はありません。
6. 全9問終了後、「記録CSVをダウンロード」を押します。
7. 操作履歴を残す場合は「操作履歴CSVをダウンロード」を押します。記録・置換・時刻修正・取消・リセットの操作時刻を保存します。
8. 各質問欄にある日時入力から時刻を修正できます。「直前の記録操作を取り消す」は、新規記録・置換なら新しい記録を削除し、時刻修正なら修正前の時刻に戻します。置換前のイベントは復元しません。

質問終了を記録する前は、その質問の回答終了ボタンは無効です。時刻記録中に一時的に無効になるのは処理中のボタンだけで、ほかの質問の記録は続けられます。記録済みのイベントを再度記録すると確認が表示され、「はい」の場合は古いイベントと紐づく操作履歴を削除して新しい記録を作成します。画面を再読み込みしても、同じセッションIDで開始すればサーバーに保存済みのイベントを再読み込みします。

## 被験者側Pythonシステムへの接続
サーバーURLは配置先のURLに置き換えてください。
```python
import threading
from sync_client import KotoSyncClient

SERVER_URL = "https://your-approved-server.example"
SESSION_ID = "P001"
sync_client = KotoSyncClient(SERVER_URL, SESSION_ID)
sync_client.start_session()
video_start_server_epoch = sync_client.mark_video_started()

stop_sync = threading.Event()
def on_event(event, video_seconds):
    print(event["event_type"], event["question_id"], video_seconds)
    # event_typeでイベントを区別して既存ログに保存します。

thread = threading.Thread(target=sync_client.run_poll_loop,
                          args=(on_event, stop_sync), daemon=True)
thread.start()
```

## 時刻の解釈と注意
- `server_epoch` はUNIX時刻（秒）、`server_time` はUTCのISO時刻です。`client_time` はブラウザ側の参考時刻です。
- `sync_client.event_to_video_seconds(event)` はサーバー時刻と録画開始時刻の差から動画内のおおよその時刻を算出します。
- ボタンを押すまでの反応時間、通信遅延、録画開始タイミング差が残ります。実験前にテストしてください。
- 面接官と被験者が別のPCで利用する場合、両方が到達できる同じサーバーURLが必要です。研究本番では大学が許可した環境でHTTPS運用し、Flask開発サーバーをインターネットへ直接公開しないでください。
