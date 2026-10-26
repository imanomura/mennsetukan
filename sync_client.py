"""Small client for the KOTO CLIP timestamp server.

Call start_session() once when the subject-side recording starts, then poll_events()
from a background thread. Convert event server_epoch values to video seconds using
video_start_server_epoch (the server epoch corresponding to the recording start).
"""
import time
import requests


class KotoSyncClient:
    def __init__(self, server_url: str, session_id: str, timeout: float = 3.0):
        self.server_url = server_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout
        self.last_event_id = 0
        self.video_start_server_epoch = None

    def start_session(self):
        """Create/reuse the session and return an approximate server-time anchor."""
        before = time.time()
        response = requests.post(
            f"{self.server_url}/api/session",
            json={"session_id": self.session_id}, timeout=self.timeout,
        )
        response.raise_for_status()
        after = time.time()
        data = response.json()
        # The server timestamp is a common time base; this is a coarse clock anchor.
        self.video_start_server_epoch = float(data["server_epoch"])
        return {**data, "client_request_started_epoch": before, "client_response_epoch": after}

    def mark_video_started(self):
        """Get a fresh server-time anchor immediately before starting video capture."""
        before = time.monotonic()
        response = requests.get(f"{self.server_url}/health", timeout=self.timeout)
        after = time.monotonic()
        response.raise_for_status()
        data = response.json()
        # Midpoint estimate reduces (but does not eliminate) request latency error.
        self.video_start_server_epoch = float(data["server_epoch"]) - ((after - before) / 2)
        return self.video_start_server_epoch

    def poll_events(self):
        response = requests.get(
            f"{self.server_url}/api/events",
            params={"session_id": self.session_id, "after_id": self.last_event_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        events = data.get("events", [])
        if events:
            self.last_event_id = max(int(e["id"]) for e in events)
        return events

    def event_to_video_seconds(self, event):
        if self.video_start_server_epoch is None:
            raise RuntimeError("先にmark_video_started()を呼び出してください。")
        return float(event["server_epoch"]) - self.video_start_server_epoch

    def run_poll_loop(self, callback, stop_event, interval=0.15, on_error=None):
        """Poll events until stop_event is set; callback(event, video_seconds) per event."""
        while not stop_event.is_set():
            try:
                for event in self.poll_events():
                    callback(event, self.event_to_video_seconds(event))
            except Exception as exc:
                if on_error:
                    on_error(exc)
            stop_event.wait(interval)
