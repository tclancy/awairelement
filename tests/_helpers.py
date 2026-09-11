"""Shared test doubles and helpers that would otherwise drift across modules."""

import re


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


def strip_js_comments(source):
    """`source` with `//` and `/* */` comments removed, string literals intact.

    The sync guards below assert on the *absence* of a construct, and an
    absence assertion over raw text has the failure mode the wrong way round:
    a developer who disables a line by commenting it out leaves the text in
    place, so the guard fires on a file that is now correct. A gate that
    cannot go green on correct code gets deleted, which is strictly worse than
    one that cannot go red.

    Quote-awareness is not decoration: `"http://x"` contains `//`, and a naive
    line-comment strip would silently truncate the string and every construct
    after it on that line. dashboard.js has no such URL today — this keeps the
    stripper from becoming a trap for the edit that adds one.

    Regex literals are handled for the same reason: `/it's/` would otherwise
    open a string that never closes, and `/\\/\\//` would read as a line
    comment. dashboard.js has no regex today, so this is entirely about the
    one-line future edit that adds one. `/` is disambiguated from division by
    the preceding significant token — the standard heuristic, and ample for a
    hand-written file. The unterminated-quote check at the end is the backstop
    for whatever the heuristic still gets wrong: it converts a silent
    mis-parse into an error that names its own cause, rather than letting the
    guards below go red citing the wrong one.
    """
    # A `/` starts a regex literal unless the previous significant token could
    # end a value, in which case it is division. `)` and `}` are genuinely
    # ambiguous in JS; treating them as value-enders is the conventional call
    # and is right for every form this file plausibly grows.
    value_enders = ")]}"
    keywords = (
        "return",
        "typeof",
        "case",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "instanceof",
        "do",
        "else",
        "yield",
        "await",
    )

    keyword_tail = re.compile(rf"\b(?:{'|'.join(keywords)})$")

    def starts_regex(prev):
        if prev is None or not (prev.isalnum() or prev in value_enders + "_$"):
            return True
        return bool(keyword_tail.search("".join(out)))

    out = []
    i, n = 0, len(source)
    quote = None
    prev_significant = None
    while i < n:
        char = source[i]
        if quote:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
        elif char in "\"'`":
            quote = char
            prev_significant = char
            out.append(char)
            i += 1
        elif source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif char == "/" and starts_regex(prev_significant):
            # Consume to the closing `/`. Inside a `[...]` class, `/` is literal.
            out.append(char)
            i += 1
            in_class = False
            while i < n and source[i] != "\n":
                c = source[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(source[i])
                    i += 1
                elif c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
            prev_significant = "/"
        else:
            out.append(char)
            if not char.isspace():
                prev_significant = char
            i += 1
    assert quote is None, (
        f"comment stripper ended inside an unterminated {quote!r} string — "
        "dashboard.js has most likely gained a regex literal, which this "
        "helper cannot parse. Nothing was stripped, so the sync guards below "
        "would fail citing the wrong cause. See `strip_js_comments` (#90)."
    )
    return "".join(out)
