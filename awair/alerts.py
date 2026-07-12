"""ntfy client. One retry, hard timeout, never raises into the poll loop."""

import logging
import urllib.request

log = logging.getLogger("awair.alerts")

NTFY_TIMEOUT_SECONDS = 10
ATTEMPTS = 2
# urllib defaults to `Python-urllib/<ver>`, which Cloudflare's bot protection
# rejects with a 403 — every notification sent through the tunnel was silently
# dropped for a day and a half before anyone noticed, because a push that never
# arrives looks exactly like "no alerts". The poller now reaches ntfy on the
# loopback and never crosses a WAF, but identify ourselves anyway: a caller who
# does point this at a public endpoint shouldn't inherit the footgun.
#
# `homelab/<ver> (<app>)` is the estate-wide convention: the shared prefix is
# allow-listable at Cloudflare in a single rule, while the parenthesized app
# keeps ntfy and WAF logs attributable to one service.
USER_AGENT = "homelab/1.0 (awairelement)"


class Notifier:
    def __init__(self, base_url, topic, token, opener=None):
        self.url = f"{base_url.rstrip('/')}/{topic}"
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def send(self, message, title="", priority="default"):
        headers = {"Priority": priority, "User-Agent": USER_AGENT}
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
