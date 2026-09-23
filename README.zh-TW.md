# ask-poteto

[English](README.md) | **繁體中文**

一個 [Claude Code](https://claude.com/claude-code) 技能（skill）：**診斷某次對話卡在哪裡，以及下一步該建什麼，讓同樣的問題不再發生。**

給它一次對話，或同一件工作被重開的好幾次對話。它會讀完整份紀錄，包括你的發言和每一次工具呼叫。每當你糾正 Claude、或 Claude 出錯，它都問一句：*這個錯本來可以在哪一層被擋下？*

1. **程式碼庫**：專案的程式碼、檔案結構、既有範例
2. **自動檢查**：測試、lint、hooks、`settings.json` 權限
3. **規則**：`CLAUDE.md`、`~/.claude/rules/`、記憶檔
4. **技能**：`~/.claude/skills/`、專案技能、外掛技能
5. **人工糾正**：你在對話裡親自提醒。這應該是最後手段。

接著用三根「信任支柱」替專案做體檢：

- **驗證**：代理能不能自己證明做對了？
- **品質技能**：它的做法符不符合你的期待，而不只是結果對？
- **對代理友善的結構**：專案是否讓它預設就做對？

每條建議都會標明**該放全域設定還是單一專案**。全域設定會影響你所有的專案，所以預設放在專案層。

> **出處與聲明**
> 「糾正代理時先想哪一層」的框架與三支柱，出自 **poteto（Lauren Tan）** 的演講：<https://x.com/poteto/status/2102050467505430555>。
> 本專案是**非官方**的整理與改寫，**與 Lauren Tan 本人無關，也未經其認可**。對原始想法的理解若有錯誤，責任在我，請以原演講為準。

## 語言

技能內容以**繁體中文**撰寫，Claude 讀起來沒有問題。報告會**用你使用的語言**回覆，預設是繁體中文。

## 需求

- Claude Code
- Python 3.8 以上，只用標準函式庫，不需要 `pip install`
- macOS 或 Linux。Windows 理論上可用，但沒有測過。

## 安裝

**所有專案都能用**（使用者層）：

```bash
git clone https://github.com/geomingical/ask-poteto.git ~/.claude/skills/ask-poteto
```

**只給單一專案用**：

```bash
git clone https://github.com/geomingical/ask-poteto.git .claude/skills/ask-poteto
```

裝好後重開 Claude Code，或開一個新對話。輸入 `/` 應該會看到 `ask-poteto`。

更新：`git -C ~/.claude/skills/ask-poteto pull`

## 使用方式

在 Claude Code 裡：

```
/ask-poteto                     # 列出最近的對話讓你挑，不會直接開始診斷
/ask-poteto current             # 診斷目前這個對話（到上一輪為止）
/ask-poteto 3f2a9c1b            # 診斷某一次對話（ID 給前幾碼即可）
/ask-poteto 3f2a9c1b 8e7d6c5a   # 同一件工作重開的多次對話，依時間合併一起診斷
```

也可以直接用白話說，例如「診斷那次對話」「為什麼 Claude 一直做錯」「檢查我的 CLAUDE.md 有沒有問題」。

運作流程：

1. 內附腳本把對話紀錄壓成一份純文字摘要，放在暫存資料夾：保留完整對話、縮短工具呼叫、丟掉圖片、遮蔽看起來像金鑰的字串。
2. Claude 列出當時生效的指示檔，分成全域與專案。
3. 派一個只能讀檔的 subagent（另一個有乾淨閱讀空間的 Claude 助手），依 [`references/rubric.md`](references/rubric.md) 的準則讀摘要、寫診斷。
4. 主 Claude 抽查引用的原文證據，整理成給你看的報告，再刪掉暫存摘要。

**它只提建議，不會改你的檔案。** 要你同意後才動手。

### 單獨使用腳本

```bash
python3 scripts/condense_session.py list --project my-app --limit 20
python3 scripts/condense_session.py digest <session-id> [<更多 ID> ...] --out /tmp/digest.md
```

## 隱私

- 全部在本機執行。腳本只讀 `~/.claude/projects/`（Claude Code 存放對話紀錄的地方），摘要只寫到你指定的位置。
- 摘要含有完整對話內容。金鑰遮蔽是**盡力而為**：能抓到常見的 token 格式和 `KEY=...` 這類寫法，但不是全部。不要把摘要分享出去。
- 技能不會自己挑對話來讀，只診斷你指定的對話。

## 限制

- Claude Code 的對話紀錄格式（`.jsonl`）不是公開文件化的格式，版本更新可能會變。如果摘要變空或缺內容，歡迎開 issue。
- 「疑似糾正」標記只是關鍵字比對，會漏掉真正的糾正，也會誤標一般問句。
- 單次對話樣本很小，只看過一次的問題，技能會把建議標為低信心。

## 檔案結構

```
ask-poteto/
├── SKILL.md                     # Claude 照著做的流程
├── references/rubric.md         # 給 subagent 讀的診斷準則
└── scripts/condense_session.py  # 對話紀錄 → Markdown 摘要
```

## 授權

[MIT](LICENSE)
