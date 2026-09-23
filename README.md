# ask-poteto

**English** | [繁體中文](README.zh-TW.md)

A [Claude Code](https://claude.com/claude-code) skill that works out **where a conversation went wrong**, and what to build so it doesn't happen again.

Give it one session, or several sessions that continue the same piece of work. It reads the full log, including your messages and every tool call. Each time you had to correct Claude, or Claude got something wrong, it asks: *which layer should have caught this?*

1. **Codebase**: the project's code, file structure, existing examples
2. **Automated checks**: tests, linters, hooks, `settings.json` permissions
3. **Rules**: `CLAUDE.md`, `~/.claude/rules/`, memory files
4. **Skills**: `~/.claude/skills/`, project skills, plugin skills
5. **Human correction**: you reminding Claude in the chat. This should be the last resort.

Next it checks the project against three pillars of trust:

- **Verification**: can the agent prove its own work is correct?
- **Quality skills**: does it work the way you expect, not just produce the right result?
- **Agent-friendly structure**: does the project make the right thing the default?

Each suggestion says **whether it belongs in your global config or in one project**. Global config affects every project you have, so the skill puts suggestions in the project by default.

> **Credit and disclaimer**
> The "which layer to fix first" framework and the three pillars come from a talk by **poteto (Lauren Tan)**: <https://x.com/poteto/status/2102050467505430555>.
> This is an **unofficial** adaptation. It is **not affiliated with or endorsed by** Lauren Tan, and any errors in how the ideas are interpreted are mine. Please refer to the original talk.

## Language

The skill's instructions are written in **Traditional Chinese**. Claude reads them without trouble, and the report comes back **in the language you use**. By default that is Traditional Chinese.

## Requirements

- Claude Code
- Python 3.8+, standard library only (no `pip install` needed)
- macOS or Linux. Windows should work but hasn't been tested.

## Installation

**For all your projects** (user-level):

```bash
git clone https://github.com/geomingical/ask-poteto.git ~/.claude/skills/ask-poteto
```

**For one project only**:

```bash
git clone https://github.com/geomingical/ask-poteto.git .claude/skills/ask-poteto
```

Restart Claude Code, or start a new session. Type `/` and you should see `ask-poteto` listed.

To update: `git -C ~/.claude/skills/ask-poteto pull`

## Usage

In Claude Code:

```
/ask-poteto                     # lists recent sessions for you to pick from; does not start diagnosing
/ask-poteto current             # diagnose the current conversation (up to the previous turn)
/ask-poteto 3f2a9c1b            # diagnose one session (the first few characters of the ID are enough)
/ask-poteto 3f2a9c1b 8e7d6c5a   # diagnose several sessions that continue one piece of work, merged in time order
```

You can also just ask in plain language, for example "diagnose that session", "why does Claude keep getting this wrong?", or "check my CLAUDE.md for problems".

What happens:

1. The bundled script condenses the session log into a text-only digest in a temporary folder. It keeps the full conversation, shortens tool calls, drops images, and masks things that look like API keys.
2. Claude lists which instruction files were in effect, sorted into global and project-level.
3. A read-only subagent (a helper Claude with a clean context window) reads the digest against [`references/rubric.md`](references/rubric.md) and writes the diagnosis.
4. The main Claude spot-checks the quoted evidence, rewrites the report for you, and deletes the temporary digest.

**It only makes suggestions and never edits your files.** Changes are made only after you agree.

### Using the script on its own

```bash
python3 scripts/condense_session.py list --project my-app --limit 20
python3 scripts/condense_session.py digest <session-id> [<more-ids> ...] --out /tmp/digest.md
```

## Privacy

- Everything runs locally. The script only reads `~/.claude/projects/`, where Claude Code keeps session logs, and writes the digest to wherever you point it.
- The digest contains your full conversation text. Key masking is **best-effort**: it catches common token formats and `KEY=...` assignments, not everything. Don't share digests.
- The skill never picks sessions on its own. It diagnoses only the sessions you name.

## Limitations

- Claude Code's session log format (`.jsonl`) is not a documented public format and may change between versions. If the digest comes out empty or incomplete, please open an issue.
- The "possible correction" markers are just keyword matches. They miss real corrections and flag ordinary questions.
- A single session is a small sample. The skill labels suggestions based on one session as low confidence.

## File structure

```
ask-poteto/
├── SKILL.md                     # the workflow Claude follows
├── references/rubric.md         # diagnostic rubric read by the subagent
└── scripts/condense_session.py  # session log → Markdown digest
```

## License

[MIT](LICENSE)
