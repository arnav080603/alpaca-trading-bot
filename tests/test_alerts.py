import requests

from bot.alerts import Discord


class Resp:
    def __init__(self, code, body=None):
        self.status_code, self.text, self._body = code, "", body or {}

    def json(self):
        return self._body


def recorder(*codes):
    calls, codes = [], list(codes)

    def post(url, json, timeout):
        calls.append(json["content"])
        c = codes.pop(0)
        if isinstance(c, Exception):
            raise c
        return Resp(c, {"retry_after": 0})
    return post, calls


def test_no_webhook_does_not_send():
    post, calls = recorder()
    assert Discord(None, post=post).send("hi") is False and calls == []


def test_entry_and_exit_messages_have_the_spec_fields():
    post, calls = recorder(204, 204)
    d = Discord("https://x", post=post)
    assert d.entry("mean_reversion", "SPY", "long", 4.5, 512.3, 508.1)
    assert d.exit("mean_reversion", "SPY", "long", 4.5, 515.0, 508.1, -12.15, "z_cross")
    assert "SPY" in calls[0] and "4.5" in calls[0] and "$512.30" in calls[0] and "$508.10" in calls[0]
    assert "-$12.15" in calls[1] and "z_cross" in calls[1]


def test_retries_then_succeeds_and_never_raises():
    post, calls = recorder(requests.ConnectionError("down"), 500, 204)
    assert Discord("https://x", backoff=0, post=post).error("boom") is True and len(calls) == 3
    post, calls = recorder(500, 500, 500)
    assert Discord("https://x", backoff=0, post=post).error("boom") is False


def test_rate_limit_waits_and_retries():
    post, calls = recorder(429, 204)
    assert Discord("https://x", backoff=0, post=post).send("x") is True and len(calls) == 2


def test_mention_prefixed_and_allowed():
    sent = []
    d = Discord("https://x", mention="@everyone", post=lambda url, json, timeout: sent.append(json) or Resp(204))
    d.send("hi")
    assert sent[0]["content"] == "@everyone hi" and "everyone" in sent[0]["allowed_mentions"]["parse"]
