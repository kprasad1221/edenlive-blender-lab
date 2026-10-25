"""Local, bounded, redacted diagnostics. No telemetry or automatic uploads."""

import datetime
import json
import os
import re
import tempfile
import threading
import uuid
from collections import deque


class Diagnostics:
    def __init__(self):
        self.attempt_id = ""
        self.path = ""
        self.events = deque(maxlen=500)
        self.secrets = []
        self._lock = threading.RLock()

    def redact(self, value):
        text = str(value)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "<redacted>")
        text = re.sub(r"rk_[A-Za-z0-9_-]+", "<redacted-key>", text)
        text = re.sub(r"(?i)Bearer\s+\S+", "Bearer <redacted>", text)
        text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<redacted-token>", text)
        # Strip URLs, including signed upload URLs and query-string tokens.
        text = re.sub(r"https?://[^\s\"'<>]+", "<url>", text)
        # Traceback file locations and user filesystem paths are not useful in public reports.
        text = re.sub(r'File "[^"]+"', 'File "<path>"', text)
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "<path>", text)
        text = re.sub(r"/(?:home|Users|tmp|var|workspace)/[^\s\"'<>]+", "<path>", text)
        return text

    def protect(self, value):
        if value and value not in self.secrets:
            self.secrets.append(value)

    def start(self):
        self.attempt_id = uuid.uuid4().hex[:12]
        folder = os.path.join(tempfile.gettempdir(), "edenlive-lab")
        try:
            os.makedirs(folder, exist_ok=True)
            self.path = os.path.join(folder, f"{self.attempt_id}.jsonl")
        except OSError:
            self.path = ""

    def event(self, stage, message):
        with self._lock:
            entry = {
                "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "attempt": self.attempt_id,
                "stage": stage,
                "message": self.redact(message),
            }
            self.events.append(entry)
            if self.path:
                try:
                    if os.path.exists(self.path) and os.path.getsize(self.path) >= 2 * 1024 * 1024:
                        return  # Keep the current 500 events in memory; bound disk growth.
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry) + "\n")
                except OSError:
                    self.path = ""  # Retain the in-memory report if disk logging is unavailable.

    def snapshot(self):
        with self._lock:
            # Re-redact old entries after a newly supplied key/prompt/path is registered.
            return json.loads(self.redact(json.dumps(list(self.events))))
