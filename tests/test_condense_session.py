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
