"""Regression tests for scripts/condense_session.py (standard library only).

Run: python3 -m unittest discover -s tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "condense_session.py"
sys.path.insert(0, str(SCRIPT.parent))
import condense_session as cs  # noqa: E402
import session_sources as src  # noqa: E402

LONG_REPLY = "開頭。" + "很長的說明。" * 1200 + "結尾的測試證據"


def user(content, **extra):
    return {"type": "user", "timestamp": "2026-10-13T01:00:00Z", "message": {"role": "user", "content": content}, **extra}


def tool_use(tool_id, name, payload):
    return {"type": "assistant", "timestamp": "2026-10-13T01:01:00Z",
            "message": {"model": "m", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": payload}]}}


RECORDS = [
    {"type": "attachment", "cwd": "/tmp/proj", "version": "9.9",
     "attachment": {"type": "instructions", "files": [{"path": "/home/u/.claude/CLAUDE.md", "type": "User", "content": "x"}]}},
    {"type": "attachment", "attachment": {"type": "nested_memory", "path": "/home/u/.claude/rules/a.md", "content": {}}},
    user("<local-command-caveat>caveat</local-command-caveat>", isMeta=True),
    user("<command-name>/login</command-name>\n<command-message>login</command-message>\n<command-args></command-args>"),
    user("<local-command-stdout>Login successful, not wrong</local-command-stdout>"),
    user("This session is being continued from a previous conversation. Summary...", isCompactSummary=True),
    user("為什麼不先跑測試？", origin={"kind": "human"}),
    tool_use("t1", "Bash", {"command": "pytest -q || true", "description": "Run tests"}),
    {"type": "assistant", "timestamp": "2026-10-13T01:02:00Z", "message": {"model": "m", "content": [{"type": "text", "text": LONG_REPLY}]}},
    user("好，再來", origin={"kind": "human"}),
    user("<command-name>/ask-poteto</command-name>\n<command-message>ask-poteto</command-message>\n<command-args>不對，看另一次</command-args>"),
]


class CondenseSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = Path(cls.tmp.name) / "abc123.jsonl"
        cls.log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in RECORDS) + "\n", encoding="utf-8")
        out = Path(cls.tmp.name) / "digest.md"
        subprocess.run([sys.executable, str(SCRIPT), "digest", str(cls.log), "--out", str(out)], check=True, capture_output=True)
        cls.digest = out.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_lists_loaded_instruction_files(self):
        self.assertIn("/home/u/.claude/CLAUDE.md", self.digest)
        self.assertIn("/home/u/.claude/rules/a.md", self.digest)

    def test_shows_actual_command_not_only_description(self):
        self.assertIn("🔧 Bash：`pytest -q || true`", self.digest)

    def test_slash_commands_and_compact_summary_are_not_user_turns(self):
        self.assertIn("使用者發言 3 輪", self.digest)
        self.assertIn("【使用者 #1】", self.digest)
        self.assertNotIn("【使用者 #4】", self.digest)
        self.assertIn("/login", self.digest)  # still visible, labelled as a command

    def test_first_real_turn_is_not_flagged(self):
        self.assertNotIn("第 1 輪", self.digest.split("---")[0])

    def test_assistant_reply_is_kept_in_full(self):
        self.assertIn("結尾的測試證據", self.digest)

    def test_list_hint_count_matches_digest(self):
        summary = cs.summarize_session(self.log)
        self.assertEqual(summary["user_turns"], 3)
        self.assertEqual(summary["hint_turns"], 1)

    def test_slash_command_with_request_counts_as_user_turn(self):
        self.assertIn("/ask-poteto 不對，看另一次", self.digest)
        self.assertIn("第 3 輪", self.digest.split("---")[0])  # flagged as possible correction


def codex(kind, payload, when="2026-10-13T01:00:00Z"):
    return {"timestamp": when, "type": kind, "payload": payload}


def codex_user(*texts):
    return codex("response_item", {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": t} for t in texts]})


CODEX_RECORDS = [
    codex("session_meta", {"id": "019e0000-aaaa-7000-8000-000000000001", "cwd": "/tmp/proj", "cli_version": "0.150.0", "source": "cli"}),
    codex("turn_context", {"cwd": "/tmp/proj", "model": "gpt-x", "approval_policy": "never", "sandbox_policy": {"type": "read-only"}}),
    codex_user("# AGENTS.md instructions for /tmp/proj\n\n<INSTRUCTIONS>be careful</INSTRUCTIONS>",
               "<environment_context>\n  <cwd>/tmp/proj</cwd>\n</environment_context>",
               "幫我修好登入"),
    codex_user("# Context from my IDE setup:\n\n## Active file: app.py\n\n## My request for Codex:\n不對，先跑測試"),
    codex_user("# AGENTS.md instructions\n\n<INSTRUCTIONS>global rules</INSTRUCTIONS>"),
    codex_user("<skill>\n<name>tdd</name>\n<path>/home/u/.codex/skills/tdd/SKILL.md</path>\nbody</skill>"),
    codex("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好，我來跑測試。"}]}),
    codex("response_item", {"type": "function_call", "name": "exec_command", "call_id": "c1",
                            "arguments": json.dumps({"cmd": "pytest -q || true", "justification": "run tests"})}),
    codex("response_item", {"type": "function_call_output", "call_id": "c1",
                            "output": "Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code 2\nOutput:\nboom"}),
    codex("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c2",
                            "input": 'const r = await tools.exec_command({cmd: "npm test 2>/dev/null && git push"});\ntext(r)'}),
    codex("response_item", {"type": "custom_tool_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "Output:\nok"}]}),
    codex("event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
    codex("compacted", {"message": "", "replacement_history": []}),
]


def omp_message(message, **extra):
    return {"type": "message", "id": "x", "parentId": None, "timestamp": "2026-10-13T02:00:00Z", "message": message, **extra}


OMP_RECORDS = [
    {"type": "session", "version": 3, "id": "01a0aaaa-bbbb-7000-8000-000000000002", "timestamp": "2026-10-13T02:00:00Z", "cwd": "/tmp/omp"},
    {"type": "title", "v": 1, "title": "修登入", "source": "auto"},
    omp_message({"role": "user", "attribution": "user", "content": [{"type": "text", "text": "幫我修好登入"}]}),
    omp_message({"role": "user", "attribution": "agent", "content": [{"type": "text", "text": "### Session context injected"}]}),
    omp_message({"role": "assistant", "model": "m-omp", "stopReason": "toolUse", "content": [
        {"type": "text", "text": "先跑測試"},
        {"type": "toolCall", "id": "t1", "name": "bash", "intent": "Run tests", "arguments": {"command": "pytest || true"}}]}),
    omp_message({"role": "toolResult", "toolCallId": "t1", "toolName": "bash", "isError": True,
                 "details": {"exitCode": 1}, "content": [{"type": "text", "text": "1 failed"}]}),
    omp_message({"role": "user", "attribution": "user", "steering": True, "content": "不對，為什麼跳過測試"}),
    omp_message({"role": "assistant", "model": "m-omp", "stopReason": "aborted", "content": []}),
    omp_message({"role": "assistant", "model": "m-omp", "stopReason": "error", "errorMessage": "usage limit reached", "content": []}),
    {"type": "compaction", "id": "c", "timestamp": "2026-10-13T02:05:00Z", "summary": "s"},
]


def write_log(folder, name, records):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def run_digest(*targets):
    out = Path(tempfile.mkdtemp()) / "digest.md"
    subprocess.run([sys.executable, str(SCRIPT), "digest", *map(str, targets), "--out", str(out)], check=True, capture_output=True)
    return out.read_text(encoding="utf-8")


class CodexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = write_log(cls.tmp.name, "rollout-2026-10-13T01-00-00-019e0000-aaaa-7000-8000-000000000001.jsonl", CODEX_RECORDS)
        cls.digest = run_digest(cls.log)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detected_as_codex(self):
        self.assertEqual(src.detect_source(self.log), "codex")
        self.assertIn("工具：Codex CLI，版本 0.150.0", self.digest)
        self.assertIn("approval=never, sandbox=read-only", self.digest)

    def test_injected_context_is_not_a_user_turn(self):
        self.assertIn("使用者發言 2 輪", self.digest)
        self.assertIn("幫我修好登入", self.digest)
        self.assertNotIn("be careful", self.digest)
        self.assertNotIn("<environment_context>", self.digest)

    def test_ide_context_keeps_only_the_request(self):
        self.assertIn("不對，先跑測試", self.digest)
        self.assertNotIn("Active file", self.digest)
        self.assertIn("第 2 輪", self.digest.split("---")[0])  # flagged as possible correction

    def test_lists_agents_md_and_skills(self):
        self.assertIn("/tmp/proj/AGENTS.md", self.digest)
        self.assertIn("技能：/home/u/.codex/skills/tdd/SKILL.md", self.digest)
        self.assertIn("可能是 ~/.codex/AGENTS.md", self.digest)  # header without a folder
        self.assertNotIn("global rules", self.digest)

    def test_shell_commands_errors_and_swallows(self):
        self.assertIn("🔧 exec_command：`pytest -q || true`", self.digest)
        self.assertIn("❌ 錯誤（exec_command）", self.digest)
        self.assertIn("🔧 exec：`npm test 2>/dev/null && git push`", self.digest)
        self.assertEqual(self.digest.count("檢查結果可能被吞掉："), 2)

    def test_interrupt_and_compaction(self):
        self.assertIn("使用者中斷了 Codex（interrupted）", self.digest)
        self.assertIn("被壓縮（compact）", self.digest)
        self.assertIn("**【Codex】", self.digest)

    def test_summary_for_list(self):
        summary = cs.summarize_session(self.log)
        self.assertEqual((summary["source"], summary["user_turns"], summary["hint_turns"]), ("codex", 2, 1))
        self.assertEqual(summary["id"], "019e0000-aaaa-7000-8000-000000000001")


class OmpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = write_log(cls.tmp.name, "-tmp-omp/2026-10-13T02-00-00-000Z_01a0aaaa-bbbb-7000-8000-000000000002.jsonl", OMP_RECORDS)
        write_log(cls.tmp.name, "-tmp-omp/2026-10-13T02-00-00-000Z_01a0aaaa-bbbb-7000-8000-000000000002/__advisor.jsonl", OMP_RECORDS[:1])
        cls.digest = run_digest(cls.log)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detected_as_omp(self):
        self.assertEqual(src.detect_source(self.log), "omp")
        self.assertIn("工具：OMP", self.digest)
        self.assertIn("OMP 不會在紀錄裡寫下載入了哪些指示檔", self.digest)

    def test_agent_injected_messages_are_not_user_turns(self):
        self.assertIn("使用者發言 2 輪", self.digest)
        self.assertNotIn("Session context injected", self.digest)
        self.assertIn("在代理還在工作時插話", self.digest)

    def test_bash_command_error_interrupt_and_api_error(self):
        self.assertIn("🔧 bash：`pytest || true`（Run tests）", self.digest)
        self.assertIn("❌ 錯誤（bash）：1 failed", self.digest)
        self.assertIn("檢查結果可能被吞掉", self.digest)
        self.assertIn("使用者中斷了 OMP", self.digest)
        self.assertIn("模型或連線錯誤：usage limit reached", self.digest)
        self.assertIn("被壓縮（compact）", self.digest)

    def test_subagent_logs_listed(self):
        self.assertIn("__advisor.jsonl", self.digest)

    def test_summary_uses_recorded_title(self):
        summary = cs.summarize_session(self.log)
        self.assertEqual((summary["source"], summary["title"], summary["hint_turns"]), ("omp", "修登入", 1))


class RetryAndShellInputTest(unittest.TestCase):
    def test_rate_limit_retries_fold_into_one_turn(self):
        retry = [omp_message({"role": "user", "attribution": "user", "content": "繼續"}),
                 omp_message({"role": "assistant", "stopReason": "error", "errorMessage": "RATE_LIMITED", "content": []})]
        with tempfile.TemporaryDirectory() as tmp:
            log = write_log(tmp, "o/2026_01a0cccc.jsonl", OMP_RECORDS[:1] + retry * 50)
            digest = run_digest(log)
        self.assertIn("使用者發言 1 輪", digest)
        self.assertIn("同一句話連送 50 次", digest)
        self.assertIn("模型或連線錯誤（連續 50 次）", digest)

    def test_bang_shell_command_is_a_note_not_a_turn(self):
        records = RECORDS[:1] + [user("<bash-input>ls</bash-input>"), user("<bash-stdout>a.txt</bash-stdout>"), user("真的問題")]
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "bang.jsonl", records))
        self.assertIn("使用者發言 1 輪", digest)
        self.assertIn("⌨ 使用者直接執行 shell：ls", digest)
        self.assertNotIn("a.txt", digest)


class MixedDigestTest(unittest.TestCase):
    def test_sessions_from_different_tools_merge_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = write_log(tmp, "abc123.jsonl", RECORDS)
            omp = write_log(tmp, "o/2026_01a0aaaa.jsonl", OMP_RECORDS)
            digest = run_digest(omp, claude)
        self.assertIn("2 個 session", digest)
        self.assertLess(digest.index("# S1 時序：Claude Code"), digest.index("# S2 時序：OMP"))


class RedactTest(unittest.TestCase):
    def test_masks_secrets(self):
        cases = [
            "postgres://admin:hunter2@db.example.com/x",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def",
            "password is hunter2",
            "OPENAI_API_KEY=abc123def456",
            "apiKey: abc123def456",
            "密碼是 hunter2",
        ]
        for text in cases:
            with self.subTest(text=text):
                masked = cs.redact(text)
                for secret in ("hunter2", "abc123def456", "eyJhbGciOiJIUzI1NiJ9"):
                    self.assertNotIn(secret, masked)

    def test_keeps_ordinary_words(self):
        for text in ["monkey = banana", "max_tokens: 4000", "the key is important"]:
            with self.subTest(text=text):
                self.assertEqual(cs.redact(text), text)


if __name__ == "__main__":
    unittest.main()
