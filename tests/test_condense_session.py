"""Regression tests for scripts/condense_session.py (standard library only).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "condense_session.py"
sys.path.insert(0, str(SCRIPT.parent))
import condense_session as cs  # noqa: E402
import session_sources as src  # noqa: E402

# Every subprocess runs with this empty home, so no test can read real session logs.
FAKE_HOME = tempfile.mkdtemp(prefix="ask-poteto-home-")
ENV = {**os.environ, "HOME": FAKE_HOME}
ENV.pop("CLAUDE_CODE_SESSION_ID", None)

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
        subprocess.run([sys.executable, str(SCRIPT), "digest", str(cls.log), "--out", str(out)],
                       check=True, capture_output=True, env=ENV)
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


def run_script(*args, env=None, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env=env or ENV, cwd=cwd)


def run_digest(*targets, extra=(), env=None):
    out = Path(tempfile.mkdtemp()) / "digest.md"
    done = run_script("digest", *targets, "--out", out, *extra, env=env)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
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
        self.assertIn("🔧 exec→shell：`npm test 2>/dev/null && git push`", self.digest)
        self.assertIn("程式內容（含 1 個 shell 指令）", self.digest)
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


# ---------------------------------------------------------------- group 1: right session, privacy

def claude_log(cwd, text, when="2026-10-13T01:00:00Z"):
    return [{"type": "user", "cwd": cwd, "timestamp": when, "origin": {"kind": "human"},
             "message": {"role": "user", "content": text}}]


class CurrentSessionTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = {**ENV, "HOME": self.home.name}
        self.work = Path(self.home.name) / "work"
        self.work.mkdir()
        projects = Path(self.home.name) / ".claude" / "projects" / "-work"
        self.mine = write_log(projects, "aaaa1111.jsonl", claude_log(str(self.work), "我這個視窗"))
        self.other = write_log(projects, "bbbb2222.jsonl", claude_log(str(self.work), "另一個視窗"))
        os.utime(self.mine, (1, 1))  # the other window was updated more recently

    def tearDown(self):
        self.home.cleanup()

    def test_uses_the_session_id_claude_code_provides(self):
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": "aaaa1111"}
        digest = run_digest("current", extra=("--cwd", self.work), env=env)
        self.assertIn("我這個視窗", digest)
        self.assertNotIn("另一個視窗", digest)

    def test_without_session_id_it_asks_instead_of_guessing(self):
        done = run_script("digest", "current", "--cwd", self.work, "--out", Path(self.home.name) / "d.md", env=self.env)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("aaaa1111", done.stderr)
        self.assertIn("bbbb2222", done.stderr)


def codex_meta_record(session, cwd="/tmp/proj", parent=None):
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"
    return codex("session_meta", {"id": session, "cwd": cwd, "cli_version": "0.150.0", "source": source})


class CodexPrivacyTest(unittest.TestCase):
    def test_single_file_does_not_open_other_days_logs(self):
        with tempfile.TemporaryDirectory() as home:
            day = Path(home) / ".codex" / "sessions" / "2026" / "10" / "13"
            parent_id = "019e0000-aaaa-7000-8000-00000000000a"
            target = write_log(day, f"rollout-2026-10-13T01-00-00-{parent_id}.jsonl",
                               [codex_meta_record(parent_id), codex_user("幫我修")])
            write_log(day, "rollout-2026-10-13T01-05-00-019e0000-aaaa-7000-8000-00000000000b.jsonl",
                      [codex_meta_record("019e0000-aaaa-7000-8000-00000000000b", parent=parent_id)])
            locked = write_log(Path(home) / ".codex" / "sessions" / "2026" / "09" / "01",
                               "rollout-2026-09-01T01-00-00-019e0000-aaaa-7000-8000-00000000000c.jsonl",
                               [codex_meta_record("019e0000-aaaa-7000-8000-00000000000c", parent=parent_id)])
            locked.chmod(0)
            try:
                digest = run_digest(target, env={**ENV, "HOME": home})
            finally:
                locked.chmod(0o600)
        self.assertIn("00000000000b", digest)  # helper agent from the same day is still found
        self.assertNotIn("00000000000c", digest)


class ProjectFilterTest(unittest.TestCase):
    def test_chinese_keyword_does_not_match_everything(self):
        self.assertFalse(cs.project_matches("堰塞湖", "/work/payroll-private"))
        self.assertFalse(cs.project_matches("！！", "/work/payroll-private"))
        self.assertTrue(cs.project_matches("堰塞湖", "/work/堰塞湖研究"))
        self.assertTrue(cs.project_matches("my_app", "-Users-me-my-app"))


SECRET = "Bearer eyJhbGciOiJIUzI1NiJ9.abc.def"


class RedactEverywhereTest(unittest.TestCase):
    def test_no_secret_reaches_digest_or_list(self):
        records = [
            codex_meta_record("019e0000-aaaa-7000-8000-0000000000d1"),
            codex_user(f"幫我呼叫 API，header 是 {SECRET}"),
            codex("response_item", {"type": "function_call", "name": "exec_command", "call_id": "c1",
                                    "arguments": json.dumps({"cmd": "curl x", "justification": f"use {SECRET}"})}),
            codex("event_msg", {"type": "turn_aborted", "reason": f"stopped {SECRET}"}),
        ]
        omp = [OMP_RECORDS[0], omp_message({"role": "assistant", "stopReason": "toolUse", "content": [
            {"type": "toolCall", "id": "t1", "name": "bash", "intent": f"call with {SECRET}", "arguments": {"command": "ls"}}]})]
        with tempfile.TemporaryDirectory() as tmp:
            codex_path = write_log(tmp, "rollout-2026-10-13T01-00-00-019e0000-aaaa-7000-8000-0000000000d1.jsonl", records)
            omp_path = write_log(tmp, "o/2026_01a0dddd.jsonl", omp)
            digest = run_digest(codex_path, omp_path)
            title = cs.summarize_session(codex_path)["title"]
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", digest)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", title)


# ---------------------------------------------------------------- group 2: keep the evidence

def claude_tool(tool_id, command, result, is_error=False):
    return [tool_use(tool_id, "Bash", {"command": command}),
            {"type": "user", "timestamp": "2026-10-13T01:01:30Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                      "content": result, "is_error": is_error}]}}]


class EvidenceTest(unittest.TestCase):
    def test_passing_check_output_is_kept(self):
        records = RECORDS[:1] + claude_tool("t1", "pytest -q", "lots of dots\n" * 50 + "17 passed in 0.3s") \
            + claude_tool("t2", "ls", "a.txt")
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "ev.jsonl", records))
        self.assertIn("17 passed", digest)
        self.assertIn("成功，輸出未列出", digest)  # the ls result is omitted, but visibly so

    def test_codex_exit_status_variants(self):
        failing = [
            {"output": json.dumps({"exit_code": 2, "output": "1 failed"})},
            {"output": json.dumps({"output": "boom", "metadata": {"exit_code": -1}})},
            {"output": [{"type": "input_text", "text": json.dumps({"exit_code": 1, "output": "x"})}]},
            {"output": json.dumps({"isError": True, "output": "bad"})},
            {"output": "Process exited with code -9\nOutput:\nkilled"},
        ]
        for payload in failing:
            with self.subTest(payload=payload):
                self.assertEqual(src.codex_output(payload)[1], "error")
        self.assertEqual(src.codex_output({"output": "Process exited with code 0\nOutput:\nok"})[1], "ok")
        self.assertEqual(src.codex_output({"output": "just text"}, shell=True)[1], "unknown")

    def test_codex_code_mode_keeps_non_shell_steps(self):
        code = ('await tools.exec_command({cmd: "pytest"});\n'
                'await tools.apply_patch("*** Begin Patch\\n*** Delete File: report.md\\n*** End Patch");')
        records = [codex_meta_record("019e0000-aaaa-7000-8000-0000000000e1"), codex_user("修"),
                   codex("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": code})]
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "rollout-2026-10-13T01-00-00-019e0000-aaaa-7000-8000-0000000000e1.jsonl", records))
        self.assertIn("`pytest`", digest)
        self.assertIn("report.md", digest)

    def test_rejection_reason_counts_as_correction(self):
        reason = ("The user doesn't want to proceed with this tool use. The tool use was rejected. "
                  "To tell you how to proceed, the user said:\n不要推送，先給我看")
        records = RECORDS[:1] + claude_log("/tmp/proj", "幫我推") + claude_tool("t1", "git push", reason, is_error=True)
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "rej.jsonl", records))
        signals = digest.split("---")[0]
        self.assertIn("使用者拒絕並說明：不要推送，先給我看", signals)

    def test_user_repeating_after_interrupt_is_not_a_retry(self):
        said = omp_message({"role": "user", "attribution": "user", "content": "不要發布"})
        aborted = omp_message({"role": "assistant", "stopReason": "aborted", "content": []})
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "o/2026_01a0eeee.jsonl", OMP_RECORDS[:1] + [said, aborted, said]))
        self.assertIn("使用者發言 2 輪", digest)
        self.assertNotIn("自動重試", digest)


# ---------------------------------------------------------------- group 3: workflow and scale

class OmpRulesTest(unittest.TestCase):
    def test_injected_rules_do_not_hide_unknown_instruction_files(self):
        records = OMP_RECORDS[:1] + [{"type": "ttsr_injection", "timestamp": "2026-10-13T02:00:01Z",
                                      "injectedRules": ["no-force-push"]}]
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "o/2026_01a0ffff.jsonl", records))
        self.assertIn("OMP 不會在紀錄裡寫下載入了哪些指示檔", digest)
        self.assertIn("臨時套用的規則", digest)
        self.assertIn("no-force-push", digest)


class SinceTest(unittest.TestCase):
    def test_since_keeps_original_turn_numbers(self):
        records = (claude_log("/tmp/proj", "第一句", "2026-10-13T01:00:00Z")
                   + claude_log("/tmp/proj", "第二句", "2026-10-13T03:00:00Z"))
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "since.jsonl", records), extra=("--since", "2026-10-13T02:00:00Z"))
        self.assertNotIn("第一句", digest)
        self.assertIn("【使用者 #2】", digest)
        self.assertIn("已省略", digest)

    def test_first_timestamp_reads_only_the_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "head.jsonl", claude_log("/tmp/proj", "x", "2026-10-13T05:00:00Z"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{broken json that must never be parsed\n")
            self.assertEqual(src.first_timestamp(path), "2026-10-13T05:00:00Z")


# ---------------------------------------------------------------- re-review follow-ups

class ReReviewTest(unittest.TestCase):
    def test_known_session_id_never_falls_back_to_another_log(self):
        with tempfile.TemporaryDirectory() as home:
            work = Path(home) / "w"
            work.mkdir()
            write_log(Path(home) / ".claude" / "projects" / "-w", "old.jsonl", claude_log(str(work), "舊對話"))
            env = {**ENV, "HOME": home, "CLAUDE_CODE_SESSION_ID": "active-not-on-disk"}
            done = run_script("digest", "current", "--cwd", work, "--out", Path(home) / "d.md", env=env)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("active-not-on-disk", done.stderr)

    def test_codex_code_mode_failure_is_an_error(self):
        output = 'Script completed\nOutput:\n{"exit_code":1,"output":"Permission denied"}'
        self.assertEqual(src.codex_output({"output": output}, shell=True)[1], "error")
        self.assertEqual(src.codex_output({"output": "Script completed\nOutput:\ndone"}, shell=True)[1], "unknown")

    def test_masking_stays_inside_one_message(self):
        records = (claude_log("/tmp/proj", "什麼是 -----BEGIN PRIVATE KEY-----？")
                   + [{"type": "assistant", "timestamp": "2026-10-13T01:00:30Z",
                       "message": {"model": "m", "content": [{"type": "text", "text": "我沒問就刪了 report.md"}]}}]
                   + claude_log("/tmp/proj", "那 -----END PRIVATE KEY----- 呢", "2026-10-13T01:01:00Z")
                   + [tool_use("t1", "Bash", {"command": "export API_TOKEN=abc123def456"})])
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "pem.jsonl", records))
        self.assertIn("report.md", digest)
        self.assertIn("`export API_TOKEN=<redacted>`", digest)

    def test_codex_title_is_masked_before_it_is_shortened(self):
        pem = "-----BEGIN PRIVATE KEY-----\n" + "A" * 200 + "\n-----END PRIVATE KEY-----"
        records = [codex_meta_record("019e0000-aaaa-7000-8000-0000000000f1"), codex_user(pem)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "rollout-2026-10-13T01-00-00-019e0000-aaaa-7000-8000-0000000000f1.jsonl", records)
            title = cs.summarize_session(path)["title"]
        self.assertNotIn("AAAA", title)

    def test_delayed_check_result_links_back_to_its_command(self):
        records = [
            codex_meta_record("019e0000-aaaa-7000-8000-0000000000f2"), codex_user("跑測試"),
            codex("response_item", {"type": "function_call", "name": "exec_command", "call_id": "c1",
                                    "arguments": json.dumps({"cmd": "pytest -q"})}),
            codex("response_item", {"type": "function_call_output", "call_id": "c1",
                                    "output": "Chunk ID: 1\nProcess running with session ID 4242\nOutput:\n..."}),
            codex("response_item", {"type": "function_call", "name": "write_stdin", "call_id": "c2",
                                    "arguments": json.dumps({"session_id": 4242, "chars": ""})}),
            codex("response_item", {"type": "function_call_output", "call_id": "c2",
                                    "output": "Process exited with code 0\nOutput:\n" + "." * 2000 + "\n17 passed"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "rollout-2026-10-13T01-00-00-019e0000-aaaa-7000-8000-0000000000f2.jsonl", records))
        self.assertIn("17 passed", digest)
        self.assertIn("`pytest -q`", digest.split("write_stdin")[1])

    def test_large_results_are_not_kept_whole_in_memory(self):
        big = "x" * 200_000 + " the end"
        records = RECORDS[:1] + claude_tool("t1", "cat big.log", big)
        with tempfile.TemporaryDirectory() as tmp:
            log = src.read_session(write_log(tmp, "big.jsonl", records))
        result = next(e for e in log.events if e.kind == "result")
        self.assertLess(len(result.text), 10_000)
        self.assertIn("the end", result.text)
        self.assertEqual(result.length, len(big))

    def test_since_keeps_retries_after_the_cutoff(self):
        ask = lambda when: {**omp_message({"role": "user", "attribution": "user", "content": "繼續"}), "timestamp": when}
        fail = lambda when: {**omp_message({"role": "assistant", "stopReason": "error", "errorMessage": "RATE_LIMITED",
                                            "content": []}), "timestamp": when}
        records = OMP_RECORDS[:1] + [ask("2026-10-13T01:00:00Z"), fail("2026-10-13T01:00:01Z"),
                                     ask("2026-10-13T03:00:00Z"), fail("2026-10-13T03:00:01Z")]
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "o/2026_01a0abab.jsonl", records), extra=("--since", "2026-10-13T02:00:00Z"))
        self.assertIn("繼續", digest)
        self.assertIn("RATE_LIMITED", digest)

    def test_other_errors_mentioning_the_user_are_not_rejections(self):
        records = RECORDS[:1] + claude_tool("t1", "pytest", "AssertionError: the user said: hello", is_error=True)
        with tempfile.TemporaryDirectory() as tmp:
            digest = run_digest(write_log(tmp, "assert.jsonl", records))
        self.assertNotIn("⛔ 使用者拒絕並說明", digest)
        self.assertIn("AssertionError: the user said: hello", digest)


if __name__ == "__main__":
    unittest.main()
