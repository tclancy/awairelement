"""Shared test doubles that would otherwise drift across test modules."""


class FakeNotifier:
    """Drop-in Notifier. Sent messages accumulate on `.sent`."""

    def __init__(self):
        self.sent = []

    def send(self, message, title="", priority="default"):
        self.sent.append((title, message, priority))
        return True


class _NullCM:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def fake_url_opener(calls):
    """Return a urlopen-shaped callable that records `(url, timeout)` calls.

    Used as a drop-in for `urllib.request.urlopen` in tests that only care
    that a URL was hit, not what the response looked like.
    """

    def _open(url, timeout):
        calls.append((url, timeout))
        return _NullCM()

    return _open
