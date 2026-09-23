#!/usr/bin/env python3
"""Condense a Claude Code session log (.jsonl) into a readable Markdown digest.

Usage:
  condense_session.py list [--project KEYWORD] [--limit N]
  condense_session.py digest <session-id | path.jsonl> [more ...] --out FILE

Several targets (one piece of work resumed across sessions) are merged into one
digest in chronological order, tagged S1, S2, ...

The digest keeps every typed user message and assistant reply in full, shortens
tool calls and results, and marks signals worth a diagnostician's attention:
tool errors, interruptions, denied tool calls, user turns that look like
corrections, and shell commands written in a way that can hide a failure. The signals are hints for where to look, not verdicts.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

CORRECTION_HINTS = re.compile(
    r"不對|不是|錯了|有錯|為什麼|又|不要|別再|我說|重做|重來|改回|看清楚|沒有照|不行|"
    r"\bno\b|\bnot\b|wrong|stop|don'?t|again|instead|why did|i said|revert|undo",
    re.IGNORECASE,
)
# Shell idioms that can make a failed command look successful (a hint, not a verdict).
ERROR_SWALLOW_HINTS = re.compile(
    r"\|\|\s*(?:true|:)(?=\s|;|$)|2>\s*/dev/null|&>\s*/dev/null|\|\s*(?:tail|head)\b|\bset\s+\+e\b"
)
TOKEN_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
ASSIGNMENT_PATTERN = re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)(\s*[=:]\s*)\S+")
TOOL_INPUT_LIMIT = 300
TOOL_RESULT_LIMIT = 400
ERROR_RESULT_LIMIT = 1200
ASSISTANT_TEXT_LIMIT = 4000


def redact(text):
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return ASSIGNMENT_PATTERN.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", text)


def clip(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" …〔已截斷，原長 {len(text)} 字〕"


def resolve_session(target):
    path = Path(target).expanduser()
    if path.suffix == ".jsonl" and path.is_file():
        return path
    matches = sorted(PROJECTS.glob(f"*/{target}*.jsonl"))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        sys.exit(f"找不到 session：{target}（在 {PROJECTS} 底下沒有符合的 .jsonl）")
    sys.exit("有多個 session 符合，請給更完整的 ID：" + ", ".join(m.stem for m in matches))


def read_records(path):
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"警告：第 {line_number} 行不是合法 JSON，已略過（{error}）", file=sys.stderr)
    return records


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("〔圖片〕")
        return "\n".join(parts)
    return ""


def is_typed_user(record):
    if record.get("type") != "user" or record.get("isMeta"):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return False
    origin = record.get("origin") or {}
    return origin.get("kind", "human") == "human"


def fmt_time(stamp):
    if not stamp:
        return "--:--"
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return stamp[:16]


def summarize_session(path):
    records = read_records(path)
    first_user = next((r for r in records if is_typed_user(r)), None)
    typed = [r for r in records if is_typed_user(r)]
    title = next((r.get("aiTitle") for r in reversed(records) if r.get("type") == "ai-title"), "")
    return {
        "id": path.stem,
        "project": path.parent.name,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "size_mb": path.stat().st_size / 1_000_000,
        "user_turns": len(typed),
        "hint_turns": sum(1 for r in typed if CORRECTION_HINTS.search(block_text(r["message"].get("content")))),
        "title": title or clip(block_text(first_user["message"].get("content")) if first_user else "", 60),
    }


def project_dirs(keyword):
    if not keyword:
        return sorted(PROJECTS.iterdir())
    path = Path(keyword).expanduser()
    if path.is_dir():
        return [path]
    matches = [d for d in PROJECTS.iterdir() if d.is_dir() and keyword.lower() in d.name.lower()]
    if not matches:
        sys.exit(f"找不到名稱含「{keyword}」的專案資料夾（在 {PROJECTS} 底下）")
    return sorted(matches)


def first_timestamp(path):
    for record in read_records(path):
        if record.get("timestamp"):
            return record["timestamp"]
    return ""


def cmd_list(args):
    files = [f for d in project_dirs(args.project) for f in d.glob("*.jsonl")]
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    if not files:
        sys.exit("找不到任何 session 紀錄")
    print("| 開始 | 最後更新 | 專案 | Session ID | 大小 | 使用者發言 | 疑似糾正 | 標題 |")
    print("|---|---|---|---|---|---|---|---|")
    for path in files:
        s = summarize_session(path)
        started = fmt_day(first_timestamp(path))
        print(f"| {started} | {s['modified']} | {s['project']} | {s['id']} | {s['size_mb']:.1f} MB | {s['user_turns']} | {s['hint_turns']} | {s['title']} |")


def fmt_day(stamp):
    if not stamp:
        return "?"
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp[:16]


def digest_one(path, tag, keep_results):
    records = read_records(path)
    tool_names = {}
    lines, signals = [], []
    meta = {"cwd": None, "version": None, "permission": set(), "instructions": [], "models": set()}
    turn = 0

    for record in records:
        kind = record.get("type")
        meta["cwd"] = meta["cwd"] or record.get("cwd")
        meta["version"] = meta["version"] or record.get("version")
        if kind == "permission-mode":
            meta["permission"].add(record.get("permissionMode"))
        if kind == "system" and "loaded" in str(record.get("content", "")):
            meta["instructions"].append(str(record.get("content")))
        when = fmt_day(record.get("timestamp")) if record.get("timestamp") else "--"
        message = record.get("message") or {}

        if is_typed_user(record):
            turn += 1
            text = redact(block_text(message.get("content")))
            flag = ""
            if CORRECTION_HINTS.search(text) and turn > 1:
                flag = " ⚑疑似糾正"
                signals.append(f"- {tag}使用者第 {turn} 輪（{when}）疑似糾正：{clip(text, 120)}")
            if "[Request interrupted" in text:
                signals.append(f"- {tag}使用者第 {turn} 輪（{when}）中斷了 Claude")
            lines.append(f"\n## 【{tag}使用者 #{turn}】{when}{flag}\n\n{text.strip()}\n")
            continue

        if kind == "assistant":
            meta["models"].add(message.get("model"))
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(f"\n**【Claude】{when}**\n\n{clip(redact(block['text']), ASSISTANT_TEXT_LIMIT)}\n")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_names[block.get("id")] = name
                    payload = block.get("input") or {}
                    brief = payload.get("description") or payload.get("command") or payload.get("file_path") or payload.get("prompt") or json.dumps(payload, ensure_ascii=False)
                    lines.append(f"- 🔧 {name}：{clip(redact(str(brief)), TOOL_INPUT_LIMIT)}")
                    command = str(payload.get("command") or "")
                    swallow = ERROR_SWALLOW_HINTS.findall(command)
                    if swallow:
                        found = "、".join(dict.fromkeys(m.strip() for m in swallow))
                        signals.append(f"- {tag}{when} {name} ⚠ 可能吞掉錯誤的寫法（{found}）：{clip(redact(command), 150)}")
                        lines.append(f"  - ⚠ 可能吞掉錯誤的寫法：{found}")
            continue

        if kind == "user":
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                name = tool_names.get(block.get("tool_use_id"), "?")
                text = redact(block_text(block.get("content")))
                if block.get("is_error"):
                    denied = "denied" in text.lower() or "rejected" in text.lower() or "拒絕" in text
                    label = "⛔ 被拒絕" if denied else "❌ 錯誤"
                    signals.append(f"- {tag}{when} {name} {label}：{clip(text, 150)}")
                    lines.append(f"  - {label}（{name}）：{clip(text, ERROR_RESULT_LIMIT)}")
                elif keep_results:
                    lines.append(f"  - ↳ {clip(text, TOOL_RESULT_LIMIT)}")

    errors = sum("❌" in s or "⛔" in s for s in signals)
    swallows = sum("⚠" in s for s in signals)
    header = [
        f"## {tag}Session {path.stem}",
        "",
        f"- 紀錄檔：`{path}`",
        f"- 工作目錄：`{meta['cwd']}`",
        f"- Claude Code 版本：{meta['version']}；模型：{', '.join(sorted(m for m in meta['models'] if m))}",
        f"- 權限模式：{', '.join(sorted(p for p in meta['permission'] if p)) or '未記錄'}",
        f"- 使用者發言 {turn} 輪；工具錯誤／拒絕 {errors} 次；可能吞掉錯誤的指令 {swallows} 次",
    ]
    header += [f"- 載入的指示檔（系統紀錄）：{item}" for item in meta["instructions"]]
    subagent_logs = sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))
    if subagent_logs:
        header.append(f"- 另有 {len(subagent_logs)} 份 subagent 紀錄（可用同一支腳本另做摘要）：")
        header += [f"  - `{log}`" for log in subagent_logs]
    return header, lines, signals, turn


def cmd_digest(args):
    paths = list(dict.fromkeys(resolve_session(t) for t in args.targets))
    paths.sort(key=first_timestamp)
    multi = len(paths) > 1
    headers, bodies, all_signals, total_turns = [], [], [], 0
    for index, path in enumerate(paths, 1):
        tag = f"S{index} " if multi else ""
        header, lines, signals, turns = digest_one(path, tag, args.results)
        headers += header + [""]
        bodies += [f"\n# {tag}時序：{path.stem}（開始於 {fmt_day(first_timestamp(path))}）"] + lines
        all_signals += signals
        total_turns += turns

    title = f"# 對話摘要：{len(paths)} 個 session（依開始時間排序）" if multi else f"# Session 摘要：{paths[0].stem}"
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
    print(f"已寫入 {out}（{out.stat().st_size / 1000:.0f} KB，{len(paths)} 個 session，使用者 {total_turns} 輪，訊號 {len(all_signals)} 則）")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="列出最近的 session")
    p_list.add_argument("--project", help="只看專案名稱含這個關鍵字的資料夾（例如 my-app），或直接給資料夾路徑")
    p_list.add_argument("--limit", type=int, default=15)
    p_digest = sub.add_parser("digest", help="把一個或多個 session 壓成一份摘要")
    p_digest.add_argument("targets", nargs="+", help="session ID（可只給前幾碼）或 .jsonl 路徑；可給多個")
    p_digest.add_argument("--out", required=True, help="摘要輸出路徑")
    p_digest.add_argument("--results", action="store_true", help="也保留成功的工具結果（摘要會變大）")
    args = parser.parse_args()
    {"list": cmd_list, "digest": cmd_digest}[args.command](args)


if __name__ == "__main__":
    main()
