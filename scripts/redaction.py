"""Mask things that look like secrets (API keys, tokens, passwords) in session text.

Best-effort pattern matching. Mask a whole piece of text before shortening it:
a cut through a secret can leave a fragment no pattern recognises.

Every step runs in time proportional to the text. Patterns that let the regex
engine rescan a long run (nested repeats, or "BEGIN ... END" with no END) once
took minutes on a large tool output, so names, URLs and keys are found by
locating a fixed anchor first ("=", "://", a key marker) and walking from it.
"""
import re

MASK = "<redacted>"

TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
# curl -u user:password, curl --user user:password
CLI_USER_PASSWORD = re.compile(r"(?<!\S)(-u|--user)([ \t]+[^\s:]+:)[^\s]+")
SPOKEN_PASSWORD = re.compile(r"(?i)\b(password|passwd)(\s+(?:is|=)\s+)\S+|(密碼)(\s*(?:是|為|:|：)\s*)\S+")

# ---------------------------------------------------------------- private keys

PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
KEY_BODY_LINE = re.compile(r"[A-Za-z0-9+/=]+")  # base64, including a short last line such as "AAAA=="
# One line after a BEGIN marker: base64, an encrypted-key header (Proc-Type, DEK-Info) or blank.
# Matched from a position, so a line that is not key material is rejected at its first odd character.
KEY_LINE_AT = re.compile(r"[ \t]*(?:([A-Za-z0-9+/=]+)|([A-Za-z-]+: [^\n]*))?[ \t]*(?:\n|\Z)")


def lines_after(text, pos):
    """End of the key lines that follow pos (header, blank and base64 lines)."""
    end = pos
    while pos < len(text):
        line = KEY_LINE_AT.match(text, pos)
        if not line or line.end() == pos:
            break
        if line.group(1) or line.group(2):
            end = line.end(1) if line.group(1) else line.end(2)
        pos = line.end()
    return end


def lines_before(text, pos, floor):
    """Start of the base64 lines that end at pos (the text before an END marker), not before floor."""
    start = pos
    while pos > floor:
        line_start = max(text.rfind("\n", floor, pos - 1) + 1, floor) if pos - 1 > floor else floor
        line = text[line_start:pos].strip()
        if line and not KEY_BODY_LINE.fullmatch(line):
            break
        if line:
            start = line_start
        pos = line_start
        if line_start == floor:
            break
    return start


def mask_private_keys(text):
    """A key from BEGIN to its END; a lone marker together with the key lines next to it."""
    ends = [m for m in PEM_END.finditer(text)]
    pieces, start, next_end = [], 0, 0
    for marker in PEM_BEGIN.finditer(text):
        if marker.start() < start:
            continue
        while next_end < len(ends) and ends[next_end].start() < marker.end():
            next_end += 1
        if next_end < len(ends):
            end = ends[next_end].end()
            next_end += 1
        else:
            end = lines_after(text, marker.end())
        pieces += [text[start:marker.start()], MASK]
        start = end
    text = "".join(pieces) + text[start:]
    pieces, start = [], 0
    for marker in PEM_END.finditer(text):  # an END whose BEGIN was cut off
        begin = lines_before(text, marker.start(), start)
        pieces += [text[start:begin], MASK]
        start = marker.end()
    return "".join(pieces) + text[start:]

# ---------------------------------------------------------------- credentials in URLs

URL_USER = re.compile(r"[^\s:/@]*")
URL_PASSWORD = re.compile(r"[^\s/@]*")  # unencoded ? and # do appear in passwords in error logs


def mask_url_credentials(text):
    """scheme://user:password@host -> scheme://user:<redacted>@host, whatever the lengths."""
    pieces, start, pos = [], 0, 0
    while True:
        found = text.find("://", pos)
        if found == -1:
            break
        user = URL_USER.match(text, found + 3)
        pos = user.end()
        if pos < len(text) and text[pos] == ":":
            password = URL_PASSWORD.match(text, pos + 1)
            if password.end() < len(text) and text[password.end()] == "@":
                pieces += [text[start:pos + 1], MASK]
                start = password.end()
            pos = password.end()
        pos = max(pos, found + 3)
    return "".join(pieces) + text[start:]

# ---------------------------------------------------------------- name = value

# A secret-looking keyword inside a name: OPENAI_API_KEY, apiKey, --password, ?token=.
SECRET_KEYWORD = re.compile(r"api_?key|apikey|key|token|secret|password|passwd|pwd", re.IGNORECASE)
NAME_CHARS = re.compile(r"[A-Za-z0-9_.-]*")
# "=" or ":" (or ":="), optionally after a closing quote; a YAML value may sit on the next line.
SEPARATOR = re.compile(r"""["']?[ \t]*(?::=|[=:：])\s*""")
OPTION_SEPARATOR = re.compile(r"[ \t]+")
QUOTED_VALUE = {'"': re.compile(r'"[^"\n]*"'), "'": re.compile(r"'[^'\n]*'")}
VALUE = re.compile(r"[^\s,;]+")
QUERY_VALUE = re.compile(r"[^\s&]+")  # ?token=...&next=... : "&" ends the value only in a URL query


def keyword_is_a_name_part(text, start, end):
    """True when the keyword is a whole name or a separated part of one.

    "OPENAI_API_KEY", "apiKey" and "--password" count; "monkey", "max_tokens"
    and "keyboard" do not.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    camel = before.islower() and text[start].isupper()
    left_ok = not before.isalnum() or camel
    right_ok = not after.isalnum() or (after.isupper() and text[end - 1].islower())
    return left_ok and right_ok


def name_start(text, pos, limit=64):
    """Where the name containing pos begins; looks back at most `limit` characters."""
    floor = max(0, pos - limit)
    while pos > floor and (text[pos - 1].isalnum() or text[pos - 1] in "_.-"):
        pos -= 1
    return pos


def value_end(text, pos, in_query):
    """End of the value starting at pos: up to a space (or "&" in a URL query), and further
    when it opens a quoted string that contains spaces. Whichever reaches further wins, so
    neither "'a'secret" nor "\"a b\"" leaves part of the value unmasked."""
    found = (QUERY_VALUE if in_query else VALUE).match(text, pos)
    end = found.end() if found else pos
    quote = QUOTED_VALUE.get(text[pos:pos + 1])
    if quote:
        quoted = quote.match(text, pos)
        if quoted:
            end = max(end, quoted.end())
    return end


def mask_assignments(text):
    """Mask the value after a secret-looking name and "=" or ":" (or after a --secret-option).

    Every keyword in a name is tried ("MONKEY_API_KEY": "KEY" in MONKEY fails, API_KEY
    counts). The end of the name is worked out once per name and reused, so a long name
    full of keywords is still read only once.
    """
    pieces, start, pos = [], 0, 0
    run_start, run_end = -1, -1  # the name the last keyword was found in
    while True:
        keyword = SECRET_KEYWORD.search(text, pos)
        if not keyword:
            break
        pos = keyword.end()
        if not run_start <= keyword.start() < run_end:
            run_start, run_end = keyword.start(), NAME_CHARS.match(text, keyword.end()).end()
        if not keyword_is_a_name_part(text, keyword.start(), keyword.end()):
            continue
        name_end = run_end
        separator = SEPARATOR.match(text, name_end)
        if not separator or separator.end() == name_end:
            # "--api-key VALUE": a command-line option takes its value after a space.
            option = text.startswith("--", name_start(text, keyword.start()))
            separator = OPTION_SEPARATOR.match(text, name_end) if option else None
        if not separator or separator.end() == name_end:
            pos = max(pos, name_end)
            continue
        begin = name_start(text, keyword.start())
        in_query = begin > 0 and text[begin - 1] in "?&"
        end = value_end(text, separator.end(), in_query)
        if end == separator.end():
            pos = max(pos, name_end)
            continue
        pieces += [text[start:separator.end()], MASK]
        start = pos = end
    return "".join(pieces) + text[start:]


def redact(text):
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    text = mask_private_keys(text)
    text = mask_url_credentials(text)
    text = BEARER_TOKEN.sub(lambda m: m.group(1) + MASK, text)
    text = CLI_USER_PASSWORD.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = SPOKEN_PASSWORD.sub(lambda m: (m.group(1) or m.group(3)) + (m.group(2) or m.group(4)) + MASK, text)
    return mask_assignments(text)
