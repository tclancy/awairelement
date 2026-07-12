"""ntfy client: request shape, retry-once, never raises."""

from urllib.error import URLError

from awair.alerts import USER_AGENT, Notifier


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()


def notifier(opener):
    return Notifier(
        base_url="https://ntfy.example.com",
        topic="awair",
        token="tk_secret",
        opener=opener,
    )


def test_send_posts_to_topic_with_auth_and_priority():
    opener = FakeOpener([None])
    assert notifier(opener).send("CO2 1400 ppm", title="CO2 spike", priority="high")
    request, timeout = opener.requests[0]
    assert request.full_url == "https://ntfy.example.com/awair"
    assert request.data == b"CO2 1400 ppm"
    assert request.get_header("Authorization") == "Bearer tk_secret"
    assert request.get_header("Title") == "CO2 spike"
    assert request.get_header("Priority") == "high"
    assert timeout == 10


def test_send_retries_once_then_gives_up_without_raising():
    opener = FakeOpener([URLError("down"), URLError("still down")])
    assert notifier(opener).send("msg") is False
    assert len(opener.requests) == 2


def test_send_succeeds_on_retry():
    opener = FakeOpener([URLError("blip"), None])
    assert notifier(opener).send("msg") is True


def test_send_without_token_omits_auth_header():
    opener = FakeOpener([None])
    Notifier("https://n", "awair", "", opener=opener).send("msg")
    assert opener.requests[0][0].get_header("Authorization") is None


def test_send_identifies_itself_with_a_real_user_agent():
    """Regression: urllib's default UA is `Python-urllib/<ver>`, which Cloudflare's
    bot protection 403s. Every notification sent through the tunnel was silently
    dropped from 2026-07-11 to 07-12. The poller now talks to ntfy on the loopback,
    but alerts.py must not carry a footgun for any caller that does route it through
    a WAF — so it identifies itself explicitly.
    """
    opener = FakeOpener([None])
    assert notifier(opener).send("hi")
    request, _ = opener.requests[0]
    user_agent = request.get_header("User-agent")
    assert user_agent == USER_AGENT
    assert "urllib" not in user_agent.lower()
    # Estate-wide convention: a shared `homelab/` prefix Cloudflare can
    # allow-list in one rule, plus the app name so logs stay attributable.
    assert user_agent.startswith("homelab/")
    assert "awairelement" in user_agent
