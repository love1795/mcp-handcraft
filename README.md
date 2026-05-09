# mcp-handcraft

Edgar 的本地 MCP（Model Context Protocol）Server。

讓任何支援 MCP 的 AI（Claude、OpenClaw 等）能透過 HTTP 直接操作本機電腦，包含：檔案系統、Git、系統指令、瀏覽器、Obsidian Vault、Linear、Notion、AI 代理委派、免費圖片生成。

**目前工具數量：54 個**

---

## 架構

```
mcp-handcraft/
├── server_http.py      ← 主 HTTP MCP Server（port 8765，所有工具都在這）
├── server.py           ← stdio 入口（供本地 stdio client 使用）
├── mmx_handlers.py     ← MiniMax 媒體生成 handlers
├── run.cmd             ← 啟動 stdio server
├── run_http.cmd        ← 啟動 HTTP server（不用 Doppler）
└── test_server_http.py ← smoke test
```

---

## 啟動方式

### 正常啟動（透過 Doppler 注入 secrets）

HTTP server 啟動前必須有 `MCP_API_TOKEN`，且不能是空字串或只有空白；未設定時程式會直接拒絕啟動。

```powershell
cd C:\Users\EdgarsTool\Projects\mcp-handcraft
doppler run -- python server_http.py
```

不透過 Doppler 的最小本機範例：

```powershell
$env:MCP_API_TOKEN = "replace-with-a-long-random-token"
python server_http.py
```

### 背景啟動

```powershell
Start-Process powershell -ArgumentList '-NoProfile','-Command',
  'cd "C:\Users\EdgarsTool\Projects\mcp-handcraft"; doppler run -- python server_http.py' -WindowStyle Minimized
```

### 確認運作中

```powershell
Get-NetTCPConnection -LocalPort 8765
# 或
Invoke-RestMethod http://localhost:8765/health
```

### 停止

```powershell
Stop-Process -Id (Get-NetTCPConnection -LocalPort 8765).OwningProcess -Force
```

---

## 環境需求

| 項目 | 說明 |
|------|------|
| Python | 3.11+ |
| Doppler | secrets 管理，project `handcraft-mcp`，config `prd` |
| `MCP_API_TOKEN` | HTTP server 必填 Bearer token，未設定、空字串或只有空白會 fail-fast |
| Playwright | `playwright install chromium`（browser 工具需要） |
| Claude Code | `winget install Anthropic.ClaudeCode` + `claude auth login` |
| Ollama | 本地模型執行環境 |
| mmx CLI | MiniMax 媒體生成 |

---

## 認證

所有請求需帶 Bearer Token：

```
Authorization: Bearer <MCP_API_TOKEN>
```

Token 由 Doppler 管理（`MCP_API_TOKEN`），HTTP server 啟動前必須注入此值。

---

## 工具總覽（54 個）

### 🤖 AI 代理（7）

| 工具 | 說明 |
|------|------|
| `codex_agent` | 委派任務給 Codex AI（程式碼實作、檔案編輯） |
| `gemini_agent` | 委派任務給 Gemini CLI（快速通用任務） |
| `claude_code_agent` | 委派任務給 Claude Code（複雜重構、多檔操作） |
| `ollama_agent` | 委派任務給本地 Ollama 模型（離線可用） |
| `smart_agent` | 智慧選擇最適合的 agent 執行任務 |
| `agent_job_status` | 查詢背景 agent job 進度 |
| `agent_job_list` | 列出所有背景 jobs |
| `agent_job_cleanup` | 清除已完成的舊 jobs |

> 長任務建議加 `"async": true`，先拿 `job_id`，再用 `agent_job_status` 輪詢。

---

### 📁 檔案系統（7）

| 工具 | 說明 |
|------|------|
| `fs_list` | 列出資料夾內容 |
| `fs_read` | 讀取檔案內容 |
| `fs_write` | 寫入或覆蓋檔案 |
| `fs_move` | 移動或重命名檔案/資料夾 |
| `fs_delete` | 刪除檔案（不可逆，謹慎使用） |
| `fs_search` | 全文搜尋檔案內容 |
| `fs_disk_info` | 查看磁碟使用量 |

---

### ⚙️ 系統（3）

| 工具 | 說明 |
|------|------|
| `sys_run` | 執行 PowerShell 指令（危險指令會被攔截） |
| `sys_info` | 查看 CPU、記憶體、系統資訊 |
| `sys_processes` | 列出執行中的程序 |

> `sys_run` 內建黑名單，會阻擋 `format`、`diskpart`、`del /f /s /q c:\` 等破壞性指令。

---

### 🔧 Git（4）

| 工具 | 說明 |
|------|------|
| `git_status` | 查看 repo 狀態（modified/untracked/staged） |
| `git_log` | 查看 commit 歷史 |
| `git_diff` | 查看變更內容 |
| `git_commit` | 建立 commit |

---

### 🌐 瀏覽器（3）

| 工具 | 說明 |
|------|------|
| `browser_screenshot` | 對網頁截圖，存到 `.screenshots/` |
| `browser_get_text` | 擷取網頁純文字內容 |
| `browser_run_script` | 在網頁上執行 JavaScript |

> 需要 Playwright + Chromium：`playwright install chromium`

---

### 🔍 網路搜尋（1）

| 工具 | 說明 |
|------|------|
| `web_search` | 用 Perplexity AI 搜尋，回傳含引用來源的結果 |

---

### 📋 Linear（3）

| 工具 | 說明 |
|------|------|
| `linear_issues` | 列出 issues（可篩選狀態/優先級） |
| `linear_create_issue` | 建立新 issue |
| `linear_update_issue` | 更新 issue 狀態或新增留言 |

---

### 📝 Notion（2）

| 工具 | 說明 |
|------|------|
| `notion_get_page` | 讀取 Notion 頁面內容 |
| `notion_search` | 搜尋 Notion workspace |

---

### 🖼 圖片生成（1）

| 工具 | 說明 |
|------|------|
| `image_generate_free` | 免費圖片生成（Pollinations.AI，不需 API key），存為 PNG 到 `.screenshots/` |

> 模型選項：`flux`（預設，高品質）、`turbo`（快速）、`gptimage`

---

### 🎬 MiniMax 媒體（8，需付費帳號）

| 工具 | 說明 |
|------|------|
| `mmx_image_generate` | 生成圖片 |
| `mmx_video_generate` | 生成影片 |
| `mmx_speech_synthesize` | 文字轉語音 |
| `mmx_music_generate` | 生成音樂 |
| `mmx_vision_describe` | 圖片描述 |
| `mmx_search_query` | MiniMax 搜尋 |
| `mmx_text_chat` | MiniMax 對話 |
| `mmx_quota_show` | 查看剩餘額度 |

---

### 📓 Obsidian Vault（13）

Vault 路徑：`D:\Edgar'sObsidianVault`

| 工具 | 說明 |
|------|------|
| `vault_read` | 讀取筆記內容 |
| `vault_write` | 建立或覆蓋筆記 |
| `vault_append` | 在筆記末尾附加內容 |
| `vault_list` | 列出資料夾內容 |
| `vault_search` | 全文搜尋所有筆記 |
| `vault_delete` | 刪除筆記（移到 .trash，可復原） |
| `vault_move` | 移動或重命名筆記 |
| `vault_daily_note` | 取得或建立今日日記 |
| `vault_recent` | 列出最近修改的筆記 |
| `vault_tasks` | 列出所有未完成任務（- [ ]） |
| `vault_tags` | 列出所有 tags 及使用次數 |
| `vault_create_from_template` | 用模板建立新筆記 |
| `vault_sort_inbox` | **自動整理 Inbox**：掃描散落筆記，依內容分類搬到正確 PARA 資料夾 |

#### Vault 結構（PARA 方法）

```
00 Inbox/          ← 先丟這裡，之後用 vault_sort_inbox 整理
01 Projects/       ← 正在進行的專案
02 Areas/          ← 持續維護的領域（AI環境、架構、工具）
03 Resources/      ← 參考資料、指令、指南
04 Archive/        ← 封存的舊內容
Templates/         ← 筆記模板
```

#### 可用模板

| 模板名稱 | 用途 |
|---------|------|
| `Daily Notes` | 每日日記 |
| `AI 任務卡` | 多代理 AI 任務追蹤（對應 Agent-KB 格式） |
| `Agent 交接備忘` | Agent 間任務移交紀錄 |
| `每日 Agent 彙整` | 每日 Agent 使用總結 |
| `工具研究筆記` | 新工具評估記錄 |
| `Meeting Notes` | 會議記錄 |
| `Weekly Review` | 每週回顧 |
| `Decision Record` | 架構決策記錄（ADR 格式） |
| `Project` | 專案追蹤 |
| `Learning Project` | 學習專案 |
| `Research Clipping` | 網路資料剪輯 |
| `Resource` | 工具/文件資源 |

---

## Smoke Test

```powershell
cd C:\Users\EdgarsTool\Projects\mcp-handcraft
doppler run -- python -m unittest test_server_http.py -v
```

---

## 環境變數（由 Doppler 管理）

| 變數 | 說明 |
|------|------|
| `MCP_API_TOKEN` | 必填 Bearer Token 認證；未設定、空字串或只有空白時 HTTP server 會拒絕啟動 |
| `PERPLEXITY_API_KEY` | web_search 用 |
| `OPENAI_API_KEY` | 備用 |
| `LINEAR_API_KEY` | Linear issue 管理 |
| `NOTION_API_KEY` | Notion 讀取 |
| `MCP_AGENT_TIMEOUT_SECONDS` | Agent 等待上限（預設 300 秒） |
| `MCP_BASE_URL` | 公開 URL（預設 https://mcp.whoasked.vip） |

---

## 公開端點

```
https://mcp.whoasked.vip/mcp
```

透過 Cloudflare Tunnel 對外。本機重開機後需手動重啟 cloudflared。

---

## 相關連結

- Linear Project：WHO 系列 issues
- Agent-KB：`D:\Agent-KB`
- Vault：`D:\Edgar'sObsidianVault`
- Screenshots：`C:\Users\EdgarsTool\Projects\mcp-handcraft\.screenshots\`
