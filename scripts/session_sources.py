"""Read Claude Code, Codex CLI and OMP session logs into one common shape.

Each reader turns a .jsonl log into a SessionLog: metadata plus a list of
Events in time order. condense_session.py renders that shape as Markdown; this
module does no formatting. Tool results are the one exception to "no redaction":
they are masked whole and then trimmed here, so huge outputs never stay in
memory and a trim can never cut a secret in half before it is masked.

None of the three formats is a documented public format. The readers keep only
what the digest needs and skip records they do not recognise.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from redaction import redact

CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOTS = (Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions")
OMP_ROOT = Path.home() / ".omp" / "agent" / "sessions"
SOURCES = ("claude", "codex", "omp")
TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex CLI", "omp": "OMP"}
AGENT_NAMES = {"claude": "Claude", "codex": "Codex", "omp": "OMP"}
MISSING_INSTRUCTIONS = {
    "claude": "紀錄裡找不到（可能是 Claude Code 紀錄格式變了），請改從工作目錄與 `~/.claude/` 推斷",
    "codex": "紀錄裡找不到 AGENTS.md 的注入紀錄，請改從工作目錄的 AGENTS.md 與 `~/.codex/AGENTS.md` 推斷",
    "omp": "OMP 不會在紀錄裡寫下載入了哪些指示檔，請從工作目錄（AGENTS.md、CLAUDE.md 等）與 `~/.omp/agent/` 推斷",
}

# Records Claude Code writes as "user" but that the person did not type as a message.
COMMAND_TAGS = ("<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>")


@dataclass(frozen=True)
class Event:
    """One thing that happened in a session, in the order it was recorded.

    kind: user, assistant, tool, result, note, compact, interrupt or api_error.
    A result's status is "ok", "error", "unknown" (the log did not say whether it worked)
    or "running" (a long command reported progress; its outcome comes in a later result).
    """
    kind: str
    when: str = ""
    text: str = ""
    name: str = ""
    payload: dict = field(default_factory=dict)
    status: str = "ok"
    call_id: str = ""  # links a tool call to its result
    length: int = 0  # original length of a result's text before it was trimmed
    times: tuple = ()  # when each folded retry happened, so a time filter can keep the later ones
    steering: bool = False  # typed while the agent was still working
    repeat: int = 1  # identical retries folded into this event


@dataclass
class SessionLog:
    source: str
    path: Path
    id: str
    cwd: str | None = None
    version: str | None = None
    models: set = field(default_factory=set)
    permission: set = field(default_factory=set)
    instructions: list = field(default_factory=list)
    injected_rules: list = field(default_factory=list)  # rules a tool applied mid-session, not files
    subagent_logs: list = field(default_factory=list)
    title: str = ""
    events: list = field(default_factory=list)

    @property
    def tool(self) -> str:
        return TOOL_NAMES[self.source]

    @property
    def agent(self) -> str:
        return AGENT_NAMES[self.source]

    @property
    def missing_instructions(self) -> str:
        return MISSING_INSTRUCTIONS[self.source]

    def add_instruction(self, path: str) -> None:
        if path and path not in self.instructions:
            self.instructions.append(path)

    def first_timestamp(self) -> str:
        return next((e.when for e in self.events if e.when), "")


def read_records(path: Path):
    """Yield records one line at a time, so a large log is never held in memory whole."""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"警告：{path.name} 第 {line_number} 行不是合法 JSON，已略過（{error}）", file=sys.stderr)
                continue
            if isinstance(record, dict):
                yield record


def head_records(path: Path, max_lines: int = 200):
    """Yield the first records of a log without reading the whole file."""
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= max_lines:
                return
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


# A result keeps at most its start and end; the digest never shows more than that.
RESULT_HEAD = 3000
RESULT_TAIL = 1500


def trim_result(text: str) -> str:
    """Keep the start and end of a long tool output so huge logs do not fill memory.

    Cuts fall on whitespace, so a secret is either kept whole (and masked later) or dropped.
    """
    if len(text) <= RESULT_HEAD + RESULT_TAIL:
        return text
    head_end = text.rfind(" ", 0, RESULT_HEAD)
    head_end = max(head_end, text.rfind("\n", 0, RESULT_HEAD))
    tail_start = len(text) - RESULT_TAIL
    found = min([i for i in (text.find(" ", tail_start), text.find("\n", tail_start)) if i != -1], default=-1)
    head = text[:head_end] if head_end > 0 else ""
    tail = text[found:] if found != -1 else ""
    return f"{head}\n…〔中間 {len(text) - len(head) - len(tail)} 字未保留〕…\n{tail}"


def result_event(when: str, text: str, name: str, status: str, call_id: str) -> Event:
    """A tool result, masked while still whole and only then trimmed."""
    return Event("result", when, trim_result(redact(text)), name, status=status, call_id=call_id, length=len(text))


def first_timestamp(path: Path) -> str:
    """Timestamp of the first timed record, read from the start of the file only."""
    return next((str(r["timestamp"]) for r in head_records(path) if r.get("timestamp")), "")


def detect_source(path: Path) -> str:
    """Tell which tool wrote a log from its first records."""
    for record in head_records(path, 20):
        if record.get("type") == "session_meta":
            return "codex"
        if record.get("type") == "session" and "cwd" in record:
            return "omp"
    return "claude"


def log_cwd(path: Path) -> str | None:
    """Working directory recorded near the start of a log, for any source."""
    for record in head_records(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        cwd = record.get("cwd") or payload.get("cwd")
        if cwd:
            return cwd
    return None


def codex_meta(path: Path) -> dict:
    record = next(head_records(path, 1), {})
    return record.get("payload") or {} if record.get("type") == "session_meta" else {}


def is_codex_subagent(path: Path) -> bool:
    """Codex stores each spawned helper agent as its own log; those are not conversations."""
    return isinstance(codex_meta(path).get("source"), dict)


def all_logs(source: str) -> list:
    """Top-level session logs of one tool (subagent logs excluded where they are separate)."""
    if source == "claude":
        return list(CLAUDE_ROOT.glob("*/*.jsonl")) if CLAUDE_ROOT.is_dir() else []
    if source == "codex":
        return [p for root in CODEX_ROOTS if root.is_dir() for p in root.rglob("rollout-*.jsonl")]
    return list(OMP_ROOT.glob("*/*.jsonl")) if OMP_ROOT.is_dir() else []


def session_id(path: Path, source: str) -> str:
    if source == "codex":
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", path.stem)
        return match.group(1) if match else path.stem
    if source == "omp":
        return path.stem.split("_", 1)[-1]
    return path.stem


def project_label(path: Path, source: str) -> str:
    if source == "codex":
        return codex_meta(path).get("cwd") or "?"
    return path.parent.name


def find_by_id(target: str, sources) -> list:
    """(source, path) pairs whose session ID starts with target."""
    found = []
    for source in sources:
        found += [(source, p) for p in all_logs(source) if session_id(p, source).startswith(target)]
    return found


# ---------------------------------------------------------------- Claude Code

def block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("text", "input_text", "output_text"):
                parts.append(item.get("text", ""))
            elif item.get("type") in ("image", "input_image"):
                parts.append("〔圖片〕")
        return "\n".join(parts)
    return ""


def slash_command(record: dict) -> str | None:
    """Return the slash command text (e.g. "/login") or its output, else None."""
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    content = block_text((record.get("message") or {}).get("content")).strip()
    return command_note(content)


def command_note(content: str) -> str | None:
    if not content.startswith(COMMAND_TAGS):
        return None
    name = re.search(r"<command-name>(.*?)</command-name>", content, re.DOTALL)
    args = re.search(r"<command-args>(.*?)</command-args>", content, re.DOTALL)
    if name:
        return f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    output = re.sub(r"</?local-command-std(?:out|err)>", "", content).strip()
    return f"（指令輸出）{output}"


def has_typed_request(record: dict) -> bool:
    """True when a slash command carries text the person typed after it."""
    content = block_text((record.get("message") or {}).get("content"))
    args = re.search(r"<command-args>(.*?)</command-args>", content, re.DOTALL)
    return bool(args and args.group(1).strip())


def user_text(record: dict) -> str:
    """What the person typed: plain message text, or "/command request"."""
    note = slash_command(record)
    return note if note is not None else block_text(record["message"].get("content"))


def is_typed_user(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return False
    if slash_command(record) is not None and not has_typed_request(record):
        return False
    origin = record.get("origin") or {}
    return origin.get("kind", "human") == "human"


def instruction_paths(record: dict) -> list:
    """Paths of instruction files (CLAUDE.md, rules, memory) recorded as loaded."""
    attachment = record.get("attachment") or {}
    if attachment.get("type") == "instructions":
        return [f.get("path") for f in attachment.get("files") or [] if isinstance(f, dict) and f.get("path")]
    if attachment.get("type") == "nested_memory" and attachment.get("path"):
        return [attachment["path"]]
    return []


def shell_input(text: str):
    """("note", text) for a shell command the person ran with "!", ("skip", "") for its output, else None."""
    stripped = text.lstrip()
    if stripped.startswith("<bash-input>"):
        return "note", "使用者直接執行 shell：" + re.sub(r"</?bash-input>", "", stripped).strip()
    if stripped.startswith(("<bash-stdout>", "<bash-stderr>")):
        return "skip", ""
    return None


def collapse_retries(events: list) -> list:
    """Fold automatic retries: the same message re-sent after nothing but errors, and repeated identical errors.

    A rate-limited session can resend one message thousands of times; without this
    the digest would be mostly copies. An interruption in between means the person
    stopped the agent and said it again, which is a correction, so it is never folded.
    """
    folded = []
    for event in events:
        previous = folded[-1] if folded else None
        if event.kind == "api_error" and previous and previous.kind == "api_error" and previous.text == event.text:
            folded[-1] = replace(previous, repeat=previous.repeat + 1, times=(previous.times or (previous.when,)) + (event.when,))
            continue
        if event.kind == "user":
            index = len(folded) - 1
            while index >= 0 and folded[index].kind == "api_error":
                index -= 1
            if 0 <= index < len(folded) - 1 and folded[index].kind == "user" and folded[index].text == event.text:
                first = folded[index]
                folded[index] = replace(first, repeat=first.repeat + 1, times=(first.times or (first.when,)) + (event.when,))
                continue
        folded.append(event)
    return folded


def read_claude(path: Path) -> SessionLog:
    log = SessionLog("claude", path, path.stem)
    tool_names = {}
    for record in read_records(path):
        kind = record.get("type")
        log.cwd = log.cwd or record.get("cwd")
        log.version = log.version or record.get("version")
        if kind == "permission-mode" and record.get("permissionMode"):
            log.permission.add(record["permissionMode"])
        if kind == "ai-title" and record.get("aiTitle"):
            log.title = record["aiTitle"]
        for loaded in instruction_paths(record):
            log.add_instruction(loaded)
        when = record.get("timestamp") or ""
        message = record.get("message") or {}

        note = slash_command(record)
        if note is not None and not is_typed_user(record):
            log.events.append(Event("note", when, f"使用者執行斜線指令：{note}"))
        elif kind == "user" and record.get("isCompactSummary"):
            log.events.append(Event("compact", when))
        elif is_typed_user(record):
            shell = shell_input(user_text(record))
            if shell is None:
                log.events.append(Event("user", when, user_text(record)))
            elif shell[0] == "note":
                log.events.append(Event("note", when, shell[1]))
        elif kind == "assistant":
            if message.get("model"):
                log.models.add(message["model"])
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    log.events.append(Event("assistant", when, block["text"]))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_names[block.get("id")] = name
                    log.events.append(Event("tool", when, name=name, payload=block.get("input") or {},
                                            call_id=str(block.get("id") or "")))
        elif kind == "user":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = tool_names.get(block.get("tool_use_id"), "?")
                    log.events.append(result_event(when, block_text(block.get("content")), name,
                                                   "error" if block.get("is_error") else "ok",
                                                   str(block.get("tool_use_id") or "")))
    log.subagent_logs = sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))
    return log


# ---------------------------------------------------------------- Codex CLI

# Blocks Codex adds to the user's message; the person did not type them.
CODEX_CONTEXT_TAGS = (
    "<environment_context>", "<recommended_plugins>", "<task-notification>", "<subagent_notification>",
    "<heartbeat>", "<guardian_tool_descriptions>", "<external_codex_apps_open_page>",
    "<turn_aborted>",  # the interruption itself is read from the turn_aborted event
)
CODEX_CONTEXT_HEADINGS = ("# Context from my IDE setup", "# Files mentioned by the user", "# In app browser")
CODEX_REQUEST = re.compile(r"^## My request(?: for Codex)?:[ \t]*\n?", re.MULTILINE)
CODEX_IMAGE_LABEL = re.compile(r"^\s*(?:</?image[^>]*>|\[Image #\d+\])\s*$")
CODEX_EXIT = re.compile(r"Process exited with code (-?\d+)|^Exit code: (-?\d+)", re.MULTILINE)
CODEX_SHELL_TOOLS = ("exec_command", "shell", "shell_command")
CODEX_RUNNING = re.compile(r"Process running with session ID (\d+)")
# Shell commands written inside Codex "code mode" scripts: tools.exec_command({cmd: "..."}).
CODEX_EMBEDDED_CMD = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)""")
PATCH_FILE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: ([^\n\"'\\]+)")


def codex_user_part(text: str):
    """Classify one text item of a Codex user message: (kind, value)."""
    stripped = text.lstrip()
    if stripped.startswith("# AGENTS.md instructions"):
        folder = re.sub(r"^# AGENTS\.md instructions(?: for )?", "", stripped.splitlines()[0]).strip()
        if not folder:
            return "instructions", "AGENTS.md（紀錄沒寫是哪個資料夾的，可能是 ~/.codex/AGENTS.md）"
        return "instructions", f"{folder.rstrip('/')}/AGENTS.md"
    if stripped.startswith("<skill>"):
        found = re.search(r"<path>(.*?)</path>", stripped) or re.search(r"<name>(.*?)</name>", stripped)
        return "instructions", f"技能：{found.group(1).strip()}" if found else ""
    if stripped.startswith(CODEX_CONTEXT_TAGS) or CODEX_IMAGE_LABEL.match(text):
        return "context", ""
    shell = shell_input(stripped)
    if shell is not None:
        return ("note", shell[1]) if shell[0] == "note" else ("context", "")
    note = command_note(stripped)
    if note is not None:
        return "note", f"使用者執行斜線指令：{note}"
    request = CODEX_REQUEST.search(stripped)
    if request:
        return "typed", stripped[request.end():]
    if stripped.startswith(CODEX_CONTEXT_HEADINGS):
        return "context", ""
    return "typed", text


def unquote(literal: str) -> str:
    if literal.startswith('"'):
        try:
            return json.loads(literal)
        except json.JSONDecodeError:
            pass
    return literal[1:-1].replace("\\n", "\n").replace("\\'", "'").replace("\\`", "`")


def codex_shell_command(name: str, args: dict) -> str:
    command = args.get("cmd") or args.get("command") or ""
    if isinstance(command, list):  # ["bash", "-lc", "..."]
        command = command[-1] if len(command) >= 3 and command[-2] in ("-lc", "-c") else " ".join(map(str, command))
    return str(command)


def codex_tool_events(when: str, payload: dict) -> list:
    name = payload.get("name", "?")
    call_id = str(payload.get("call_id") or "")
    if payload.get("type") == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"arguments": payload.get("arguments")}
        if not isinstance(args, dict):
            args = {"arguments": args}
        command = codex_shell_command(name, args) if name in CODEX_SHELL_TOOLS else ""
        if command:
            args = {"command": command, "description": args.get("justification") or ""}
        elif name == "write_stdin" and args.get("session_id") is not None:
            # What was typed into the running command matters (it may be "git push --force").
            args = {"session_id": str(args["session_id"]), "chars": str(args.get("chars") or "")}
        return [Event("tool", when, name=name, payload=args, call_id=call_id)]
    code = str(payload.get("input") or "")
    commands = [unquote(m) for m in CODEX_EMBEDDED_CMD.findall(code)]
    files = list(dict.fromkeys(f.strip() for f in PATCH_FILE.findall(code)))
    if not commands:
        detail = "修改檔案：" + "、".join(files) if files and name == "apply_patch" else code
        return [Event("tool", when, name=name, payload={"description": detail}, call_id=call_id)]
    # A script can do more than run shell commands (edit or delete files, call other tools).
    # Keep the whole script; the extracted commands are extra hints for the swallow check.
    summary = f"程式內容（含 {len(commands)} 個 shell 指令" + (f"，動到檔案：{'、'.join(files)}" if files else "") + "）"
    events = [Event("tool", when, name=name, payload={"code": code, "description": summary}, call_id=call_id)]
    return events + [Event("tool", when, name=f"{name}→shell", payload={"command": c}) for c in commands]


def codex_json(text: str):
    """The tool output as a dict when it is a JSON object, else None."""
    if not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


JSON_DECODER = json.JSONDecoder()
# A failed parse can read far ahead (deeply nested, never closed), so stop after a few;
# the result is then "unknown" rather than read as success.
MAX_JSON_FAILURES = 20


def result_fields(obj, depth: int = 0):
    """(exit codes, error flags, running process IDs) of one tool-result structure.

    When the structure reports its own exit code, its "output" is the command's
    stdout and may contain anything, so it is not read as a status. Only a bare
    wrapper (no exit code of its own) is followed into its "output".
    """
    codes, errors, running = [], [], []
    if not isinstance(obj, dict) or depth > 3:
        return codes, errors, running
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    code = obj.get("exit_code")
    if code is None:
        code = metadata.get("exit_code")
    if isinstance(code, int):
        codes.append(code)
    if obj.get("isError") or obj.get("is_error"):
        errors.append(True)
    if obj.get("session_id") is not None and code is None:
        running.append(str(obj["session_id"]))
    if code is None:
        inner = obj.get("output")
        if isinstance(inner, str):
            inner = codex_json(inner)
        for found, extra in zip((codes, errors, running), result_fields(inner, depth + 1)):
            found += extra
    return codes, errors, running


LINE_START_BRACE = re.compile(r"^[ \t]*\{", re.MULTILINE)
SPACES = re.compile(r"[ \t]*")
# The status lines Codex puts before a direct shell call's output. Everything after them is the
# command's own stdout, which is never read as a status.
CODEX_HEADER_LINE = re.compile(
    r"(?:Chunk ID|Wall time|Process exited with code|Process running with session ID|Original token count"
    r"|Exit code|Total output lines)\b")


def shell_header(text: str) -> str:
    """The leading status lines of a direct shell result."""
    lines = []
    for line in text.lstrip("\r\n").split("\n", 12)[:12]:
        if not CODEX_HEADER_LINE.match(line.strip()):
            break
        lines.append(line)
    return "\n".join(lines)


# A result printed anywhere in a script's output: an object with a key, or a list of objects.
# "[INFO]" or "{x}" in ordinary log text is not mistaken for one.
JSON_START = re.compile(r'\{(?=\s*")|\[(?=\s*\{)')


def json_blocks(text: str, anywhere: bool = False):
    """(position, object) for each JSON result in text, including multi-line ones and
    the objects inside a printed list, plus how many could not be parsed. One pass.

    By default only objects that start a line are read; `anywhere` also reads a result
    printed after a label ("step 1: {...}") or as a list ("[{...}, {...}]").
    """
    blocks, unparsed, pos = [], 0, 0
    starts = JSON_START.finditer(text) if anywhere else LINE_START_BRACE.finditer(text)
    for match in starts:
        brace = match.end() - 1
        if brace < pos:  # inside an object already read
            continue
        if unparsed >= MAX_JSON_FAILURES:
            break
        # Several results can share a line; read each, and treat anything else left on the line as unreadable.
        while True:
            try:
                obj, end = JSON_DECODER.raw_decode(text, brace)
            except (ValueError, RecursionError):
                unparsed += 1
                break
            if isinstance(obj, dict):
                blocks.append((brace, obj))
            elif isinstance(obj, list):
                blocks += [(brace, item) for item in obj if isinstance(item, dict)]
            pos = end
            if anywhere:  # later results on the line are found by the scan itself; labels are fine
                break
            following = SPACES.match(text, end).end()
            if following >= len(text) or text[following] == "\n":
                break
            if text[following] not in "{[":
                unparsed += 1
                break
            brace = following
    return blocks, unparsed


def codex_result_facts(text: str, code_mode: bool):
    """Exit codes, error flags, running process IDs (in the order reported) and whether some
    result could not be read, for one Codex tool call.

    A direct shell call reports its status in a header before "Output:"; what comes
    after is the command's own stdout, which may contain anything (a test fixture
    with "exit_code": 1), so it is not read. A code-mode script prints one result
    per step, so the whole output is read, including results spread over several lines.
    """
    parsed = codex_json(text)
    if parsed is not None:
        codes, errors, running = result_fields(parsed)
        inner = parsed.get("output")
        if not codes and isinstance(inner, str) and codex_json(inner) is None:
            codes = [int(a or b) for a, b in CODEX_EXIT.findall(shell_header(inner))]
        return codes, errors, running, False
    region = text if code_mode else shell_header(text)
    codes, errors, ordered = [], [], []
    for match in CODEX_EXIT.finditer(region):
        codes.append(int(match.group(1) or match.group(2)))
    for match in CODEX_RUNNING.finditer(region):
        ordered.append((match.start(), match.group(1)))
    blocks, unparsed = json_blocks(region, anywhere=code_mode)
    for position, obj in blocks:
        more = result_fields(obj)
        codes += more[0]
        errors += more[1]
        ordered += [(position, process) for process in more[2]]
    running = [process for _, process in sorted(ordered)]
    return codes, errors, running, unparsed > 0


def codex_output(payload: dict, shell: bool = False, code_mode: bool = False):
    """(text, status) for a Codex tool output record: "ok", "error", "running" or "unknown".

    Any failing step fails the call, and a failure is never hidden behind "still
    running". Shell results whose exit status cannot be found, and scripts with a
    result that could not be read, are "unknown", never assumed to have worked.
    """
    text = block_text(payload.get("output"))
    code_mode = code_mode or text.lstrip().startswith(("Script completed", "Script error"))
    codes, errors, running, unreadable = codex_result_facts(text, code_mode)
    parsed = codex_json(text)
    if parsed is not None and "output" in parsed:
        inner = parsed["output"]
        text = inner if isinstance(inner, str) else block_text(inner)
    failed = any(errors) or any(code != 0 for code in codes) \
        or bool(re.search(r"^Script error:", text, re.MULTILINE)) or "rejected by user" in text.lower()
    if failed:
        return text, "error"
    if running:  # a step still running means the call is not finished, even if another step is
        return text, "running"
    if (shell and not codes) or unreadable:
        return text, "unknown"
    return text, "ok"


def running_ids(payload: dict, code_mode: bool = False) -> list:
    """Process IDs a Codex result says are still running, in the order they appear."""
    text = block_text(payload.get("output"))
    code_mode = code_mode or text.lstrip().startswith(("Script completed", "Script error"))
    return list(dict.fromkeys(codex_result_facts(text, code_mode)[2]))


def read_codex(path: Path) -> SessionLog:
    log = SessionLog("codex", path, session_id(path, "codex"))
    tool_names = {}
    commands = {}  # call id -> shell command it ran
    shell_calls = set()  # code-mode calls that ran shell commands
    script_steps = {}  # code-mode call id -> the shell commands it ran, in order
    running = {}  # Codex process session ID -> command still running, for write_stdin results
    for record in read_records(path):
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        what = payload.get("type")
        when = record.get("timestamp") or ""
        if kind == "session_meta":
            log.id = payload.get("id") or log.id
            log.cwd = log.cwd or payload.get("cwd")
            log.version = log.version or payload.get("cli_version")
        elif kind == "turn_context":
            log.cwd = log.cwd or payload.get("cwd")
            if payload.get("model"):
                log.models.add(payload["model"])
            sandbox = (payload.get("sandbox_policy") or {}).get("type")
            log.permission.add(f"approval={payload.get('approval_policy')}, sandbox={sandbox}")
        elif kind == "compacted":
            log.events.append(Event("compact", when))
        elif kind == "event_msg" and what == "turn_aborted":
            log.events.append(Event("interrupt", when, str(payload.get("reason") or "")))
        elif kind == "response_item" and what == "message" and payload.get("role") == "user":
            typed = []
            for item in payload.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "input_image":
                    typed.append("〔圖片〕")
                    continue
                part, value = codex_user_part(item.get("text") or "")
                if part == "instructions":
                    log.add_instruction(value)
                elif part == "note":
                    log.events.append(Event("note", when, value))
                elif part == "typed" and value.strip():
                    typed.append(value)
            if typed:
                log.events.append(Event("user", when, "\n".join(typed)))
        elif kind == "response_item" and what == "message" and payload.get("role") == "assistant":
            text = block_text(payload.get("content"))
            if text.strip():
                log.events.append(Event("assistant", when, text))
        elif kind == "response_item" and what in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id")
            tool_names[call_id] = payload.get("name", "?")
            events = codex_tool_events(when, payload)
            if events[0].payload.get("command"):
                commands[call_id] = events[0].payload["command"]
            steps = [e.payload["command"] for e in events if e.name.endswith("→shell")]
            if steps:
                shell_calls.add(call_id)
                script_steps[call_id] = steps
                commands[call_id] = " ；".join(steps)
            if events[0].name == "write_stdin" and events[0].payload.get("session_id") in running:
                earlier = running[events[0].payload["session_id"]]
                commands[call_id] = earlier
                events[0] = replace(events[0], payload={**events[0].payload, "continues": earlier})
            log.events += events
        elif kind == "response_item" and what in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            name = tool_names.get(call_id, "?")
            shell = name in CODEX_SHELL_TOOLS or name == "write_stdin" or call_id in shell_calls
            text, status = codex_output(payload, shell=shell, code_mode=call_id in shell_calls)
            ids = running_ids(payload, code_mode=call_id in shell_calls)
            steps = script_steps.get(call_id, [])
            for index, process in enumerate(ids):
                if call_id not in commands:
                    break
                # Pair processes with script steps in order when the counts match; otherwise link to all.
                # A process keeps the command that started it; a later mention does not move it.
                running.setdefault(process, steps[index] if len(ids) == len(steps) else commands[call_id])
            log.events.append(result_event(when, text, name, status, str(call_id or "")))
    # Full text; the list masks it before shortening, so a cut never leaves half a secret.
    log.title = next((e.text.strip() for e in log.events if e.kind == "user"), "")
    log.subagent_logs = codex_children(path, log.id)
    return log


def codex_children(path: Path, parent_id: str) -> list:
    """Helper-agent logs this session spawned.

    Only logs in the same folder (Codex groups logs by day) are opened, and only
    their first line, so diagnosing one session does not read the user's other
    sessions. A helper started after midnight lands in the next day's folder and
    is missed; the digest header says so.
    """
    children = []
    for sibling in sorted(path.parent.glob("rollout-*.jsonl")):
        if sibling == path:
            continue
        try:
            if codex_parent(sibling) == parent_id:
                children.append(sibling)
        except OSError as error:
            print(f"警告：無法讀取 {sibling.name}，略過（{error}）", file=sys.stderr)
    return children


def codex_parent(path: Path) -> str | None:
    source = codex_meta(path).get("source")
    if not isinstance(source, dict):
        return None
    return ((source.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id")


# ---------------------------------------------------------------- OMP

def omp_payload(arguments: dict, intent: str | None) -> dict:
    payload = dict(arguments or {})
    if intent and not payload.get("description"):
        payload["description"] = intent
    return payload


def read_omp(path: Path) -> SessionLog:
    log = SessionLog("omp", path, session_id(path, "omp"))
    for record in read_records(path):
        kind = record.get("type")
        when = record.get("timestamp") or ""
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        role = message.get("role")
        if kind == "session":
            log.id = record.get("id") or log.id
            log.cwd = log.cwd or record.get("cwd")
            log.version = log.version or f"紀錄格式 v{record.get('version')}"
        elif kind in ("title", "title_change") and record.get("title"):
            log.title = record["title"]
        elif kind == "mode_change" and record.get("mode"):
            log.permission.add(f"mode={record['mode']}")
        elif kind == "model_change" and record.get("model"):
            log.models.add(str(record["model"]))
        elif kind == "ttsr_injection":
            for rule in record.get("injectedRules") or []:
                if rule not in log.injected_rules:
                    log.injected_rules.append(str(rule))
        elif kind == "compaction":
            log.events.append(Event("compact", when))
        elif kind == "branch_summary":
            log.events.append(Event("note", when, "對話在這裡切換到另一個分支（branch），之前的分支由摘要取代"))
        elif role == "user" and message.get("attribution", "user") == "user":
            log.events.append(Event("user", when, block_text(message.get("content")), steering=bool(message.get("steering"))))
        elif role == "bashExecution":
            log.events.append(Event("note", when, f"使用者直接執行 shell：{message.get('command')}（結束碼 {message.get('exitCode')}）"))
        elif role == "assistant":
            if message.get("model"):
                log.models.add(message["model"])
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    log.events.append(Event("assistant", when, block["text"]))
                elif block.get("type") == "toolCall":
                    log.events.append(Event("tool", when, name=block.get("name", "?"),
                                            payload=omp_payload(block.get("arguments"), block.get("intent")),
                                            call_id=str(block.get("id") or "")))
            if message.get("stopReason") == "aborted":
                log.events.append(Event("interrupt", when))
            elif message.get("stopReason") == "error":
                log.events.append(Event("api_error", when, str(message.get("errorMessage") or "")))
        elif role == "toolResult":
            exit_code = (message.get("details") or {}).get("exitCode") if isinstance(message.get("details"), dict) else None
            failed = bool(message.get("isError")) or exit_code not in (None, 0)
            log.events.append(result_event(when, block_text(message.get("content")), message.get("toolName", "?"),
                                           "error" if failed else "ok", str(message.get("toolCallId") or "")))
    log.subagent_logs = sorted((path.parent / path.stem).glob("*.jsonl"))
    return log


READERS = {"claude": read_claude, "codex": read_codex, "omp": read_omp}


def read_session(path: Path, source: str | None = None) -> SessionLog:
    log = READERS[source or detect_source(path)](path)
    log.events = collapse_retries(log.events)
    return log
