"""ntfy client. One retry, hard timeout, never raises into the poll loop."""

import logging
import urllib.request

log = logging.getLogger("awair.alerts")

NTFY_TIMEOUT_SECONDS = 10
ATTEMPTS = 2


class Notifier:
    def __init__(self, base_url, topic, token, opener=None):
        self.url = f"{base_url.rstrip('/')}/{topic}"
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def send(self, message, title="", priority="default"):
        headers = {"Priority": priority}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if title:
            headers["Title"] = title
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(
                self.url, data=message.encode(), headers=headers, method="POST"
            )
            try:
                with self.opener(request, timeout=NTFY_TIMEOUT_SECONDS):
                    return True
            except OSError as exc:
                log.warning("ntfy send failed (attempt %d): %s", attempt, exc)
        return False
