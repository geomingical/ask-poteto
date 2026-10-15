"""Mask things that look like secrets (API keys, tokens, passwords) in session text.

Best-effort pattern matching. Mask a whole piece of text before shortening it:
a cut through a secret can leave a fragment no pattern recognises.
"""
import re

TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
# Private keys are found by scanning from their markers (see mask_private_keys), not by
# one regex: a "BEGIN ... END" regex rescans to the end of the text for every unmatched
# BEGIN, which took over 30 seconds on a large output.
PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]{16,}")
# A secret-looking name followed by "=" or ":". The keyword must be the whole name
# or a separated part of it (OPENAI_API_KEY, apiKey), so "monkey" and "max_tokens" stay.
SECRET_NAME = r"(?:[A-Za-z0-9]+[_-])*(?:api_?key|key|token|secret|password|passwd|pwd)(?:[_-][A-Za-z0-9]+)*"
# Matching runs in two steps so a long run like "a_a_a_..." stays fast: find a short name
# (at most 80 characters, starting at a word boundary) before "=" or ":", then check the name.
ASSIGNMENT_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{1,80})(\s*[=:：]\s*)([^\s,;]+)")
SECRET_NAME_PATTERN = re.compile(SECRET_NAME, re.IGNORECASE)
SPOKEN_PASSWORD = re.compile(r"(?i)\b(password|passwd)(\s+(?:is|=)\s+)\S+|(密碼)(\s*(?:是|為|:|：)\s*)\S+")
# Lengths are bounded so a long run without spaces cannot make matching slow.
URL_CREDENTIALS = re.compile(r"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s:/@]{1,256}:)[^\s@/]{1,256}@", re.IGNORECASE)
BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def mask_private_keys(text):
    """Mask private keys: BEGIN to its END, or a lone marker with the base64 lines next to it."""
    ends = [m.end() for m in PEM_END.finditer(text)]
    pieces, start, next_end = [], 0, 0
    for marker in PEM_BEGIN.finditer(text):
        if marker.start() < start:
            continue
        while next_end < len(ends) and ends[next_end] <= marker.end():
            next_end += 1
        if next_end < len(ends):  # a whole key
            pieces += [text[start:marker.start()], "<redacted>"]
            start = ends[next_end]
            continue
        end = marker.end()  # a lone start: the marker and the base64 lines after it
        while True:
            gap = len(text[end:]) - len(text[end:].lstrip())
            line = BASE64_LINE.match(text, end + gap)
            if not line:
                break
            end = line.end()
        pieces += [text[start:marker.start()], "<redacted>"]
        start = end
    text = "".join(pieces) + text[start:]
    pieces, start = [], 0
    for marker in PEM_END.finditer(text):  # lone end: walk back over whole base64 lines only
        begin = marker.start()
        while True:
            line_start = text.rfind("\n", start, begin - 1) + 1 if begin > start else begin
            line = text[line_start:begin].strip()
            if not line or not BASE64_LINE.fullmatch(line) or line_start < start:
                break
            begin = line_start
        pieces += [text[start:begin], "<redacted>"]
        start = marker.end()
    return "".join(pieces) + text[start:]


def redact(text):
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("<redacted>", text)
    text = mask_private_keys(text)
    text = URL_CREDENTIALS.sub(lambda m: m.group(1) + "<redacted>@", text)
    text = BEARER_TOKEN.sub(lambda m: m.group(1) + "<redacted>", text)
    text = SPOKEN_PASSWORD.sub(lambda m: (m.group(1) or m.group(3)) + (m.group(2) or m.group(4)) + "<redacted>", text)
    return ASSIGNMENT_CANDIDATE.sub(mask_assignment, text)


def mask_assignment(match):
    if not SECRET_NAME_PATTERN.fullmatch(match.group(1)):
        return match.group(0)
    return match.group(1) + match.group(2) + "<redacted>"
