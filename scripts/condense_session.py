#!/usr/bin/env python3
"""Condense a Claude Code, Codex CLI or OMP session log (.jsonl) into a readable Markdown digest.

Usage:
  condense_session.py list [--source all|claude|codex|omp] [--project KEYWORD] [--limit N]
  condense_session.py digest <session-id | path.jsonl | current> [more ...] --out FILE [--source ...]

Several targets (one piece of work resumed across sessions) are merged into one
digest in chronological order, tagged S1, S2, ...

The digest keeps every typed user message and assistant reply in full, shows
slash commands as one-line notes (not user turns), shortens tool calls and
results, lists the instruction files the tool loaded, and marks signals worth a diagnostician's attention:
tool errors, interruptions, denied tool calls, user turns that look like
corrections, and shell commands written in a way that can hide a failure. The signals are hints for where to look, not verdicts.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import session_sources as src

# Above this size, reading the whole digest costs noticeably more usage; ask first.
LARGE_DIGEST_KB = 300

CORRECTION_HINTS = re.compile(
    r"不對|不是|錯了|有錯|為什麼|又|不要|別再|我說|重做|重來|改回|看清楚|沒有照|不行|"
    r"\bno\b|\bnot\b|wrong|stop|don'?t|again|instead|why did|i said|revert|undo",
    re.IGNORECASE,
)
# Shell idioms that can make a failed command look successful (a hint, not a verdict).
ERROR_SWALLOW_HINTS = re.compile(
    r"\|\|\s*(?:true|:)(?=\s|;|$)|2>\s*/dev/null|&>\s*/dev/null|\|\s*(?:tail|head|grep)\b|\bset\s+\+e\b"
)
# Commands whose success matters: tests, builds, linters, type checkers.
CHECK_COMMAND = re.compile(
    r"\b(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|playwright\s+test|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|check|typecheck)|"
    r"go\s+(?:test|build|vet)|cargo\s+(?:test|build|check|clippy)|make\b|"
    r"ruff|mypy|pyright|eslint|tsc|mvn|gradle)\b"
)
# Actions that act on a result: commit, push, publish, deploy, delete, move.
FOLLOW_UP_ACTION = re.compile(
    r"&&[^|]*?\b(?:git\s+(?:commit|push|merge|tag)|gh\s+pr\s+(?:create|merge)|"
    r"(?:npm|pnpm|yarn)\s+publish|twine\s+upload|deploy|vercel|rm\s|mv\s)"
)


def risky_swallow(command):
    """Return the swallowing idioms only when they can hide a failed check.

    Flags a check command (test, build, lint) combined with an idiom that hides
    its exit status, either followed by an action that relies on the result or
    explicitly forced to succeed with `|| true`. Plain `ls | head` and similar
    output trimming are ignored.
    """
    if not CHECK_COMMAND.search(command):
        return []
    idioms = [m.strip() for m in ERROR_SWALLOW_HINTS.findall(command)]
    if not idioms or "pipefail" in command:
        return []
    forced = any(i.startswith("||") or i == "set +e" for i in idioms)
    if FOLLOW_UP_ACTION.search(command) or forced:
        return list(dict.fromkeys(idioms))
    return []
TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
# A secret-looking name followed by "=" or ":". The keyword must be the whole name
# or a separated part of it (OPENAI_API_KEY, apiKey), so "monkey" and "max_tokens" stay.
SECRET_NAME = r"(?:[A-Za-z0-9]+[_-])*(?:api_?key|key|token|secret|password|passwd|pwd)(?:[_-][A-Za-z0-9]+)*"
ASSIGNMENT_PATTERN = re.compile(rf"(?i)\b({SECRET_NAME})(\s*[=:：]\s*)[^\s,;]+")
SPOKEN_PASSWORD = re.compile(r"(?i)\b(password|passwd)(\s+(?:is|=)\s+)\S+|(密碼)(\s*(?:是|為|:|：)\s*)\S+")
URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@", re.IGNORECASE)
BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
TOOL_INPUT_LIMIT = 300
COMMAND_LIMIT = 600
TOOL_RESULT_LIMIT = 400
ERROR_RESULT_LIMIT = 1200


def redact(text):
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("<redacted>", text)
    text = URL_CREDENTIALS.sub(lambda m: m.group(1) + "<redacted>@", text)
    text = BEARER_TOKEN.sub(lambda m: m.group(1) + "<redacted>", text)
    text = SPOKEN_PASSWORD.sub(lambda m: (m.group(1) or m.group(3)) + (m.group(2) or m.group(4)) + "<redacted>", text)
    return ASSIGNMENT_PATTERN.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", text)


def clip(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" …〔已截斷，原長 {len(text)} 字〕"


def current_session(cwd, source):
    """Most recently modified session of one tool whose recorded working directory is cwd.

    Claude Code and OMP name project folders by rewriting the path, so the folder
    name cannot be rebuilt reliably (underscores, dots, spaces, non-ASCII).
    Match on the recorded cwd instead.
    """
    target = str(Path(cwd).expanduser().resolve())
    logs = sorted(src.all_logs(source), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in logs:
        if source == "codex" and src.is_codex_subagent(path):
            continue
        recorded = src.log_cwd(path)
        if recorded and str(Path(recorded).resolve()) == target:
            return source, path
    sys.exit(f"找不到工作目錄是 {target} 的 {src.TOOL_NAMES[source]} session。請改給 session ID，或先用 list 查。")


def resolve_session(target, cwd=None, source="auto"):
    """(source, path) for a session ID prefix, a .jsonl path, or "current"."""
    if target == "current":
        return current_session(cwd or Path.cwd(), "claude" if source == "auto" else source)
    path = Path(target).expanduser()
    if path.suffix == ".jsonl" and path.is_file():
        return (src.detect_source(path) if source == "auto" else source), path
    matches = src.find_by_id(target, src.SOURCES if source == "auto" else [source])
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"找不到 session：{target}（查過 Claude Code、Codex、OMP 的紀錄資料夾）")
    sys.exit("有多個 session 符合，請給更完整的 ID 或加 --source：" +
             ", ".join(f"{src.TOOL_NAMES[s]} {src.session_id(p, s)}" for s, p in matches))


def tool_brief(payload):
    """One-line view of a tool call; shell commands are shown verbatim."""
    command = payload.get("command")
    if command:
        brief = f"`{clip(redact(str(command)), COMMAND_LIMIT)}`"
        if payload.get("description"):
            brief += f"（{clip(str(payload['description']), 80)}）"
        return brief
    detail = (payload.get("file_path") or payload.get("path") or payload.get("description")
              or payload.get("prompt") or json.dumps(payload, ensure_ascii=False))
    return clip(redact(str(detail)), TOOL_INPUT_LIMIT)


def summarize_session(path, source=None):
    log = src.read_session(path, source)
    typed = [e for e in log.events if e.kind == "user"]
    return {
        "source": log.source,
        "id": log.id,
        "project": src.project_label(path, log.source),
        "started": fmt_day(log.first_timestamp()),
        "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "size_mb": path.stat().st_size / 1_000_000,
        "user_turns": len(typed),
        # Same rule as the digest: the opening message is never counted as a correction.
        "hint_turns": sum(1 for e in typed[1:] if CORRECTION_HINTS.search(e.text)),
        "title": clip(log.title or (typed[0].text if typed else ""), 60).replace("\n", " ").replace("|", "／"),
    }


def project_matches(keyword, label):
    """Keyword match that ignores how each tool rewrites paths (Claude Code turns "_" and "/" into "-")."""
    loose = lambda text: re.sub(r"[^0-9a-z]+", "-", text.lower())
    return keyword.lower() in label.lower() or loose(keyword) in loose(label)


def candidate_logs(source, keyword):
    """(source, path) of top-level logs, optionally only those whose project matches keyword."""
    sources = src.SOURCES if source == "all" else [source]
    folder = Path(keyword).expanduser() if keyword else None
    found = []
    for name in sources:
        for path in src.all_logs(name):
            if folder and folder.is_dir():
                if path.parent.resolve() != folder.resolve():
                    continue
            elif keyword and not project_matches(keyword, src.project_label(path, name)):
                continue
            found.append((name, path))
    if keyword and not found:
        sys.exit(f"找不到專案名稱或資料夾含「{keyword}」的 session")
    return sorted(found, key=lambda item: item[1].stat().st_mtime, reverse=True)


def cmd_list(args):
    rows = []
    for name, path in candidate_logs(args.source, args.project):
        if len(rows) >= args.limit:
            break
        if name == "codex" and src.is_codex_subagent(path):
            continue  # helper agents Codex spawned; they appear under their parent's digest
        rows.append(summarize_session(path, name))
    if not rows:
        sys.exit("找不到任何 session 紀錄")
    print("| 工具 | 開始 | 最後更新 | 專案 | Session ID | 大小 | 使用者發言 | 疑似糾正 | 標題 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for s in rows:
        print(f"| {src.TOOL_NAMES[s['source']]} | {s['started']} | {s['modified']} | {s['project']} | {s['id']} | "
              f"{s['size_mb']:.1f} MB | {s['user_turns']} | {s['hint_turns']} | {s['title']} |")


def fmt_day(stamp):
    if not stamp:
        return "?"
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp[:16]


def digest_one(log, tag, keep_results):
    lines, signals = [], []
    turn = 0
    errors = swallows = loose_swallows = 0
    agent = log.agent

    for event in log.events:
        when = fmt_day(event.when) if event.when else "--"
        if event.kind == "note":
            lines.append(f"- ⌨ {clip(redact(event.text), TOOL_INPUT_LIMIT)}")
        elif event.kind == "compact":
            lines.append(f"\n> 〔{when} 對話在這裡被壓縮（compact），之前的內容由系統摘要取代〕\n")
        elif event.kind == "interrupt":
            signals.append(f"- {tag}{when} 使用者中斷了 {agent}" + (f"（{event.text}）" if event.text else ""))
            lines.append(f"- ⏹ 使用者在這裡中斷了 {agent}")
        elif event.kind == "api_error":
            times = f"（連續 {event.repeat} 次）" if event.repeat > 1 else ""
            signals.append(f"- {tag}{when} 模型或連線錯誤{times}：{clip(redact(event.text), 150)}")
            lines.append(f"  - ❌ 模型或連線錯誤{times}：{clip(redact(event.text), ERROR_RESULT_LIMIT)}")
        elif event.kind == "user":
            turn += 1
            text = redact(event.text)
            flag = " ⚑疑似糾正" if CORRECTION_HINTS.search(text) and turn > 1 else ""
            if flag:
                signals.append(f"- {tag}使用者第 {turn} 輪（{when}）疑似糾正：{clip(text, 120)}")
            if "[Request interrupted" in text:
                signals.append(f"- {tag}使用者第 {turn} 輪（{when}）中斷了 {agent}")
            steering = "（在代理還在工作時插話）" if event.steering else ""
            if event.repeat > 1:
                steering += f"（同一句話連送 {event.repeat} 次，中間只有錯誤，多半是自動重試）"
            lines.append(f"\n## 【{tag}使用者 #{turn}】{when}{steering}{flag}\n\n{text.strip()}\n")
        elif event.kind == "assistant":
            lines.append(f"\n**【{agent}】{when}**\n\n{redact(event.text).strip()}\n")
        elif event.kind == "tool":
            lines.append(f"- 🔧 {event.name}：{tool_brief(event.payload)}")
            command = str(event.payload.get("command") or "")
            if ERROR_SWALLOW_HINTS.search(command):
                loose_swallows += 1
            risky = risky_swallow(command)
            if risky:
                swallows += 1
                found = "、".join(risky)
                signals.append(f"- {tag}{when} {event.name} ⚠ 檢查結果可能被吞掉（{found}）：{clip(redact(command), 150)}")
                lines.append(f"  - ⚠ 檢查結果可能被吞掉：{found}")
        elif event.kind == "result":
            text = redact(event.text)
            if event.is_error:
                lowered = text.lower()
                denied = "denied" in lowered or "rejected" in lowered or "拒絕" in text
                label = "⛔ 被拒絕" if denied else "❌ 錯誤"
                errors += 1
                signals.append(f"- {tag}{when} {event.name} {label}：{clip(text, 150)}")
                lines.append(f"  - {label}（{event.name}）：{clip(text, ERROR_RESULT_LIMIT)}")
            elif keep_results:
                lines.append(f"  - ↳ {clip(text, TOOL_RESULT_LIMIT)}")

    header = [
        f"## {tag}Session {log.id}",
        "",
        f"- 紀錄檔：`{log.path}`",
        f"- 工作目錄：`{log.cwd}`",
        f"- 工具：{log.tool}，版本 {log.version}；模型：{', '.join(sorted(m for m in log.models if m))}",
        f"- 權限模式：{', '.join(sorted(p for p in log.permission if p)) or '未記錄'}",
        f"- 使用者發言 {turn} 輪；工具錯誤／拒絕 {errors} 次；檢查結果可能被吞掉的指令 {swallows} 次",
        f"- 另有 {loose_swallows} 個指令用了 `| head`、`2>/dev/null` 等寫法，多半只是縮短輸出，未逐一列出；診斷時可視需要 grep 原始紀錄",
    ]
    if log.instructions:
        header.append("- 載入的指示檔（依紀錄，出現順序）：")
        header += [f"  - `{item}`" for item in log.instructions]
    else:
        header.append(f"- 載入的指示檔：{log.missing_instructions}")
    if log.subagent_logs:
        header.append(f"- 另有 {len(log.subagent_logs)} 份 subagent 紀錄（可用同一支腳本另做摘要）：")
        header += [f"  - `{sub}`" for sub in log.subagent_logs]
    return header, lines, signals, turn


def cmd_digest(args):
    targets = list(dict.fromkeys(resolve_session(t, args.cwd, args.source) for t in args.targets))
    logs = sorted((src.read_session(path, source) for source, path in targets), key=lambda log: log.first_timestamp())
    multi = len(logs) > 1
    headers, bodies, all_signals, total_turns = [], [], [], 0
    for index, log in enumerate(logs, 1):
        tag = f"S{index} " if multi else ""
        header, lines, signals, turns = digest_one(log, tag, args.results)
        headers += header + [""]
        bodies += [f"\n# {tag}時序：{log.tool} {log.id}（開始於 {fmt_day(log.first_timestamp())}）"] + lines
        all_signals += signals
        total_turns += turns

    title = f"# 對話摘要：{len(logs)} 個 session（依開始時間排序）" if multi else f"# Session 摘要：{logs[0].tool} {logs[0].id}"
    doc = [title, ""]
    if multi:
        doc += ["同一件工作被重開成多個 session，依時間串起來讀。編號 S1、S2… 對應下方各段。", ""]
    doc += headers
    doc += ["## 值得注意的訊號（啟發式標記，需人工判斷）", ""]
    doc += all_signals or ["- 沒有偵測到明顯訊號"]
    doc += ["", "---"] + bodies

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(doc) + "\n", encoding="utf-8")
    size_kb = out.stat().st_size / 1000
    print(f"已寫入 {out}（{size_kb:.0f} KB，{len(logs)} 個 session，使用者 {total_turns} 輪，訊號 {len(all_signals)} 則）")
    if size_kb > LARGE_DIGEST_KB:
        print(f"注意：摘要超過 {LARGE_DIGEST_KB} KB，完整讀完會用掉較多額度與時間。", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="列出最近的 session")
    p_list.add_argument("--source", choices=("all",) + src.SOURCES, default="all",
                        help="只看某個工具的紀錄：claude（Claude Code）、codex（Codex CLI）、omp（預設全部）")
    p_list.add_argument("--project", help="只看專案名稱或工作目錄含這個關鍵字的 session（例如 my-app），或直接給紀錄資料夾路徑")
    p_list.add_argument("--limit", type=int, default=15)
    p_digest = sub.add_parser("digest", help="把一個或多個 session 壓成一份摘要")
    p_digest.add_argument("targets", nargs="+", help="session ID（可只給前幾碼）、.jsonl 路徑，或 current；可給多個，可混用不同工具")
    p_digest.add_argument("--out", required=True, help="摘要輸出路徑")
    p_digest.add_argument("--results", action="store_true", help="也保留成功的工具結果（摘要會變大）")
    p_digest.add_argument("--cwd", help="搭配 current 使用：指定工作目錄（預設為目前所在資料夾）")
    p_digest.add_argument("--source", choices=("auto",) + src.SOURCES, default="auto",
                          help="紀錄來自哪個工具；auto 會從檔案內容判斷、ID 會在三個工具的資料夾都找。current 搭配 auto 時視為 Claude Code")
    args = parser.parse_args()
    {"list": cmd_list, "digest": cmd_digest}[args.command](args)


if __name__ == "__main__":
    main()
