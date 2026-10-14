"""Read Claude Code, Codex CLI and OMP session logs into one common shape.

Each reader turns a .jsonl log into a SessionLog: metadata plus a list of
Events in time order. condense_session.py renders that shape as Markdown; this
module does no formatting, redaction or clipping.

None of the three formats is a documented public format. The readers keep only
what the digest needs and skip records they do not recognise.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

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
    """
    kind: str
    when: str = ""
    text: str = ""
    name: str = ""
    payload: dict = field(default_factory=dict)
    is_error: bool = False
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


def read_records(path: Path) -> list:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"警告：{path.name} 第 {line_number} 行不是合法 JSON，已略過（{error}）", file=sys.stderr)
    return records


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
    the digest would be mostly copies.
    """
    folded = []
    for event in events:
        previous = folded[-1] if folded else None
        if event.kind == "api_error" and previous and previous.kind == "api_error" and previous.text == event.text:
            folded[-1] = replace(previous, repeat=previous.repeat + 1)
            continue
        if event.kind == "user":
            index = len(folded) - 1
            while index >= 0 and folded[index].kind in ("api_error", "interrupt"):
                index -= 1
            if 0 <= index < len(folded) - 1 and folded[index].kind == "user" and folded[index].text == event.text:
                folded[index] = replace(folded[index], repeat=folded[index].repeat + 1)
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
                    log.events.append(Event("tool", when, name=name, payload=block.get("input") or {}))
        elif kind == "user":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = tool_names.get(block.get("tool_use_id"), "?")
                    log.events.append(Event("result", when, block_text(block.get("content")), name,
                                            is_error=bool(block.get("is_error"))))
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
CODEX_EXIT = re.compile(r"Process exited with code ([1-9]\d*)|^Exit code: ([1-9]\d*)", re.MULTILINE)
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
    if payload.get("type") == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"arguments": payload.get("arguments")}
        if not isinstance(args, dict):
            args = {"arguments": args}
        command = codex_shell_command(name, args) if name in ("exec_command", "shell", "shell_command") else ""
        if command:
            args = {"command": command, "description": args.get("justification") or ""}
        return [Event("tool", when, name=name, payload=args)]
    code = str(payload.get("input") or "")
    commands = [unquote(m) for m in CODEX_EMBEDDED_CMD.findall(code)]
    if commands:
        return [Event("tool", when, name=name, payload={"command": c}) for c in commands]
    files = list(dict.fromkeys(f.strip() for f in PATCH_FILE.findall(code)))
    detail = "修改檔案：" + "、".join(files) if files else code
    return [Event("tool", when, name=name, payload={"description": detail})]


def codex_output(payload: dict):
    """(text, is_error) for a Codex tool output record."""
    output = payload.get("output")
    exit_code = None
    if isinstance(output, str) and output.startswith("{"):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "output" in parsed:
            exit_code = (parsed.get("metadata") or {}).get("exit_code")
            output = str(parsed["output"])
    text = block_text(output)
    failed = (exit_code not in (None, 0)) or bool(CODEX_EXIT.search(text)) \
        or bool(re.search(r"^Script error:", text, re.MULTILINE)) or "rejected by user" in text.lower()
    return text, failed


def read_codex(path: Path) -> SessionLog:
    log = SessionLog("codex", path, session_id(path, "codex"))
    tool_names = {}
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
            tool_names[payload.get("call_id")] = payload.get("name", "?")
            log.events += codex_tool_events(when, payload)
        elif kind == "response_item" and what in ("function_call_output", "custom_tool_call_output"):
            text, failed = codex_output(payload)
            log.events.append(Event("result", when, text, tool_names.get(payload.get("call_id"), "?"), is_error=failed))
    log.title = next((e.text.strip()[:60] for e in log.events if e.kind == "user"), "")
    log.subagent_logs = codex_children().get(log.id, [])
    return log


@lru_cache(maxsize=1)
def codex_children() -> dict:
    """Parent session ID -> logs of the helper agents it spawned (scanned once per run)."""
    children = {}
    for path in sorted(all_logs("codex")):
        parent = codex_parent(path)
        if parent:
            children.setdefault(parent, []).append(path)
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
                log.add_instruction(f"規則：{rule}")
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
                                            payload=omp_payload(block.get("arguments"), block.get("intent"))))
            if message.get("stopReason") == "aborted":
                log.events.append(Event("interrupt", when))
            elif message.get("stopReason") == "error":
                log.events.append(Event("api_error", when, str(message.get("errorMessage") or "")))
        elif role == "toolResult":
            exit_code = (message.get("details") or {}).get("exitCode") if isinstance(message.get("details"), dict) else None
            log.events.append(Event("result", when, block_text(message.get("content")), message.get("toolName", "?"),
                                    is_error=bool(message.get("isError")) or exit_code not in (None, 0)))
    log.subagent_logs = sorted((path.parent / path.stem).glob("*.jsonl"))
    return log


READERS = {"claude": read_claude, "codex": read_codex, "omp": read_omp}


def read_session(path: Path, source: str | None = None) -> SessionLog:
    log = READERS[source or detect_source(path)](path)
    log.events = collapse_retries(log.events)
    return log
