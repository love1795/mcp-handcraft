# handcraft-mcp 操作手冊

> 適用版本：0.1.0｜最後更新：2026-04-19

---

## 1. 架構一覽

```
本機
├── server.py          ← stdio 模式（Claude Desktop / OpenClaw 本機呼叫）
├── server_http.py     ← HTTP 模式（遠端 / mcp.whoasked.vip）
├── run.cmd            ← 啟動 stdio server（透過 Doppler 注入 key）
└── run_http.cmd       ← 啟動 HTTP server（透過 Doppler 注入 key）

Doppler（雲端）
└── project: handcraft-mcp / config: prd
    └── 存放所有 API key，啟動時注入，不落地

Cloudflare Tunnel
└── mcp.whoasked.vip → 本機 :8765/mcp
```

### 兩個 server 的差異

| | server.py (stdio) | server_http.py (HTTP) |
|---|---|---|
| 用途 | 本機 agent 直連 | 外網 / 遠端呼叫 |
| 啟動方式 | `run.cmd` | `run_http.cmd` |
| Port | 無（stdin/stdout） | 8765 |
| 工具數 | echo | echo + agent / Notion / MiniMax / Ollama 工具 |
| Auth | 無需 | Bearer token / OAuth metadata |

---

## 2. 啟動 / 停止

### 啟動 HTTP server（常用）
```cmd
cd C:\Users\EdgarsTool\Projects\mcp-handcraft
run_http.cmd
```

### 啟動 stdio server（本機 MCP client 用）
```cmd
run.cmd
```

### 停止
在執行中的視窗按 `Ctrl+C`。

### 確認是否在跑
```bash
# 確認 port 8765 是否被監聽
netstat -ano | findstr :8765
```

---

## 3. Secret 管理（Doppler）

### 新增 key
```bash
doppler secrets set MY_API_KEY=sk-xxxx
```

### 更新 key
```bash
doppler secrets set MY_API_KEY=sk-new-value
```

### 刪除 key
```bash
doppler secrets delete MY_API_KEY
```

### 查看目前所有 key（值會遮蔽）
```bash
doppler secrets
```

### 查看特定 key 的值
```bash
doppler secrets get MY_API_KEY
```

### Web UI
https://dashboard.doppler.com → 選 `handcraft-mcp` → `prd`

### 改完 key 要重啟 server
Doppler 在啟動時注入，改完 key 要停掉 server 重跑 `run_http.cmd`。

---

## 4. 在 server_http.py 讀取 key

server 啟動後環境變數已注入，直接讀 `os.getenv()`：

```python
import os
MY_KEY = os.getenv("MY_API_KEY", "")  # 第二個參數是預設值
```

建議在 server 最上層（import 區之後）集中宣告：

```python
# ── Secrets（由 Doppler 注入）─────────────────────────
MY_API_KEY = os.getenv("MY_API_KEY", "")
OTHER_KEY  = os.getenv("OTHER_KEY", "")
```

---

## 5. 新增 tool 流程

所有工具在 `server_http.py` 修改。兩個地方要動：

### 步驟 1：在 TOOLS 清單加定義（約第 45 行）

```python
{
    "name": "my_tool",
    "description": "說明這個工具做什麼",
    "inputSchema": {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "輸入說明"},
        },
        "required": ["input"],
    },
},
```

### 步驟 2：在 handle_tools_call 加分支（約第 569 行）

```python
if name == "my_tool":
    return handle_my_tool(req_id, arguments)
```

### 步驟 3：實作 handler function

```python
def handle_my_tool(req_id, arguments: dict) -> dict:
    val = arguments.get("input", "")
    # 你的邏輯，例如呼叫外部 API
    result = call_some_api(val)
    return make_response(req_id, make_tool_text_response(result))
```

---

## 6. 環境變數（可在 Doppler 設定）

| 變數名稱 | 預設值 | 說明 |
|---|---|---|
| `MCP_AGENT_TIMEOUT_SECONDS` | `300` | agent 指令最長執行秒數 |
| `MCP_JOB_RETENTION_SECONDS` | `3600` | 背景 job 結果保留時間（秒） |

修改方式：
```bash
doppler secrets set MCP_AGENT_TIMEOUT_SECONDS=600
```

---

## 7. 安全設定

### Bearer Token（HTTP server）

目前已經從 Doppler 讀取，不再把 token 寫死在 repo：
```python
API_TOKEN = os.getenv("MCP_API_TOKEN", "")
```

要換 token：
```bash
doppler secrets set MCP_API_TOKEN=你的新token
```

手動測試 `POST /mcp` 時，仍要自己帶：
```bash
-H "Authorization: Bearer <MCP_API_TOKEN>"
```

### OAuth metadata（相容客戶端可自動發現）

HTTP server 也會提供：

```text
GET /.well-known/oauth-authorization-server
GET /.well-known/oauth-protected-resource
GET /authorize
POST /token
POST /register
```

這是給支援 OAuth / MCP discovery 的 client 用；手動 curl 測試時仍以 Bearer token 最直接。

### Origin 白名單（DNS rebinding 防護）

在 `server_http.py` 第 216 行：
```python
ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "mcp.whoasked.vip"}
```

新增允許的 origin：
```python
ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "mcp.whoasked.vip", "new.domain.com"}
```

---

## 8. Cloudflare Tunnel

| 設定項目 | 值 |
|---|---|
| Tunnel 名稱 | `home-tunnel` |
| Tunnel ID | `0e0a1b13-db47-4d0c-9fa4-4fc7d269cbbf` |
| 對外網址 | `https://mcp.whoasked.vip` |
| 本機目標 | `http://localhost:8765` |

Tunnel 由 `cloudflared` 常駐管理，不需手動操作。確認狀態：
```bash
cloudflared tunnel info home-tunnel
```

---

## 9. Log 查看

HTTP server 的 log 輸出到 stderr（執行中的視窗）：
```
[MCP-HTTP] handcraft-mcp HTTP server starting
[MCP-HTTP] tools/call: name=codex_agent ...
[MCP-HTTP] codex_agent: exit_code=0
```

若要存到檔案：
```cmd
run_http.cmd 2> C:\Users\EdgarsTool\Projects\mcp-handcraft\mcp.log
```

---

## 10. 連線測試

```bash
# 確認 server 正常回應
curl -X POST http://localhost:8765/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MCP_API_TOKEN>" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"clientInfo\":{\"name\":\"test\",\"version\":\"1.0\"},\"capabilities\":{}}}"

# 確認外網
curl -X POST https://mcp.whoasked.vip/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MCP_API_TOKEN>" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}"
```

正常回應包含 `"serverInfo": { "name": "handcraft-mcp" }`。

---

## 11. 常見問題

**Q：改完 Doppler key，server 沒有讀到新值**
→ 停掉 server，重跑 `run_http.cmd`。Doppler 只在啟動時注入。

**Q：curl 回 403 Forbidden: Origin not allowed**
→ 把你的 origin 加進 `ALLOWED_HOSTNAMES`。

**Q：curl 回 401 Unauthorized**
→ 加上 header：`-H "Authorization: Bearer null$Orchestrator=zer0"`

**Q：agent 工具回 timeout**
→ 試試加 `"async": true` 改成背景執行，再用 `agent_job_status` 查結果。

**Q：Doppler 登入過期**
```bash
doppler login
```

**Q：確認 Doppler 綁定正確**
```bash
cd C:\Users\EdgarsTool\Projects\mcp-handcraft
doppler configure
```
應顯示 `project=handcraft-mcp config=dev`。

---

## 12. 檔案結構快查

```
mcp-handcraft/
├── server.py          stdio MCP server（skeleton）
├── server_http.py     HTTP MCP server（主力）
├── run.cmd            啟動 stdio（doppler run --）
├── run_http.cmd       啟動 HTTP（doppler run --）
├── GUIDE.md           客戶端連線設定指南
├── DOPPLER.md         Doppler 架構說明
├── OPS.md             本文件（操作手冊）
└── README.md          專案說明
```

---

## 13. 相關連結

| 資源 | 網址 |
|---|---|
| 外網端點 | https://mcp.whoasked.vip/mcp |
| Doppler Web UI | https://dashboard.doppler.com |
| Doppler 官方 fork | https://github.com/Edgars-tool/python-doppler-env |
| Cloudflare DNS | https://dash.cloudflare.com |
