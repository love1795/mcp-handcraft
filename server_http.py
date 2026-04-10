"""
手刻 MCP Server - Streamable HTTP 版本
協議版本: 2025-11-25
依賴: 僅 Python 標準庫 (http.server, json, urllib.parse)

端點: POST /mcp
回應模式: 單次 JSON（不開 SSE stream，Phase 2 基礎版）
"""

import sys
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import shutil
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

CODEX_CMD = r"C:\Users\EdgarsTool\AppData\Roaming\npm\codex.cmd"
CLAUDE_CMD = shutil.which("claude") or "claude"
GEMINI_CMD = shutil.which("gemini") or "gemini"
CODEX_DEFAULT_WORKDIR = r"C:\Users\EdgarsTool"
AGENT_TIMEOUT_SECONDS = int(os.getenv("MCP_AGENT_TIMEOUT_SECONDS", "300"))

PORT = 8765
PROTOCOL_VERSION = "2025-11-25"
API_TOKEN = "null$Orchestrator=zer0"
DEFAULT_JOB_RETENTION_SECONDS = int(os.getenv("MCP_JOB_RETENTION_SECONDS", "3600"))

SERVER_INFO = {
    "name": "handcraft-mcp",
    "version": "0.1.0",
}

JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}

TOOLS = [
    {
        "name": "echo",
        "description": "Echoes back the input message",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to echo back",
                }
            },
            "required": ["message"],
        },
    },
    {
        "name": "codex_agent",
        "description": (
            "Delegates a task to the Codex AI coding agent running on the local machine. "
            "Codex will autonomously plan, write code, run shell commands, and edit files "
            "to complete the task. Use this when you want another AI agent to handle "
            "implementation work independently. Returns Codex's final response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task or instruction for Codex to execute autonomously",
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        f"Working directory for Codex to operate in "
                        f"(default: {CODEX_DEFAULT_WORKDIR})"
                    ),
                },
                "async": {
                    "type": "boolean",
                    "description": (
                        "When true, starts the task in the background and returns a job_id "
                        "immediately. Recommended for long-running tasks or clients with short HTTP timeouts."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "gemini_agent",
        "description": (
            "Delegates a task to the Gemini CLI AI agent running on the local machine. "
            "Fast response (under 30 seconds). Best for quick coding tasks, file operations, "
            "shell commands, and general automation on the local Windows machine. "
            "Use this as the default agent for most tasks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task or instruction for Gemini to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        f"Working directory for Gemini to operate in "
                        f"(default: {CODEX_DEFAULT_WORKDIR})"
                    ),
                },
                "async": {
                    "type": "boolean",
                    "description": (
                        "When true, starts the task in the background and returns a job_id "
                        "immediately. Recommended when the client may timeout before the task finishes."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "claude_code_agent",
        "description": (
            "Delegates a task to the Claude Code AI coding agent running on the local machine. "
            "Claude Code will autonomously plan, write code, run shell commands, and edit files "
            "to complete the task. Best for complex coding, refactoring, multi-file operations, "
            "and tasks requiring deep codebase understanding. Returns Claude Code's final response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The coding task or question to send to Claude Code.",
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        f"Working directory for Claude Code to operate in "
                        f"(default: {CODEX_DEFAULT_WORKDIR})"
                    ),
                },
                "async": {
                    "type": "boolean",
                    "description": (
                        "When true, starts the task in the background and returns a job_id "
                        "immediately. Recommended for multi-minute tasks."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "agent_job_status",
        "description": (
            "Checks the status of a background agent job started with async=true. "
            "Returns queued/running/succeeded/failed plus any final output when available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by codex_agent, gemini_agent, or claude_code_agent.",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "smart_agent",
        "description": (
            "Runs a task through a fallback chain of local AI agents. "
            "Starts with Gemini for speed, then falls back to Codex, then Claude Code "
            "when quota limits, timeouts, or transient upstream failures occur. "
            "Use this as the default tool for local execution when the user wants the server "
            "to handle agent rotation automatically, especially for file edits, shell commands, "
            "or any task that may outlive a single HTTP request. Prefer async=true for remote clients."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task to execute with automatic fallback across agents.",
                },
                "working_dir": {
                    "type": "string",
                    "description": (
                        f"Working directory for the selected agent to operate in "
                        f"(default: {CODEX_DEFAULT_WORKDIR})"
                    ),
                },
                "async": {
                    "type": "boolean",
                    "description": (
                        "When true, starts the fallback workflow in the background and returns "
                        "a job_id immediately."
                    ),
                },
            },
            "required": ["task"],
        },
    },
]

# ── Origin 白名單（防 DNS rebinding，spec 強制要求）────────────────────────────
# 允許 localhost / 127.0.0.1 任意 port，供本地開發 + MCP Inspector 使用。
# Cloudflare Tunnel 接入後，瀏覽器 origin 會是 tunnel domain，需另行加入。
ALLOWED_HOSTNAMES = {"localhost", "127.0.0.1", "mcp.whoasked.vip"}


# ─── 共用工具 ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[MCP-HTTP] {msg}", file=sys.stderr, flush=True)


def make_response(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_tool_text_response(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def run_agent_command(command: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=AGENT_TIMEOUT_SECONDS,
        cwd=cwd,
        shell=False,
    )


def finalize_agent_output(
    result: subprocess.CompletedProcess,
    *,
    stdout_text: str = "",
    fallback_label: str,
) -> tuple[str, bool]:
    stdout_text = stdout_text.strip() if stdout_text else ""
    stderr_text = (result.stderr or "").strip()

    output = stdout_text or (result.stdout or "").strip()

    if result.returncode != 0:
        sections = []
        if stderr_text:
            sections.append(f"[stderr]\n{stderr_text}")
        if output:
            sections.append(f"[stdout]\n{output}")
        output = "\n".join(sections).strip()

    if not output:
        output = f"{fallback_label} exited with code {result.returncode} (no output)"

    return output, result.returncode != 0


def create_job(tool_name: str, task: str, working_dir: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "tool": tool_name,
            "task": task,
            "working_dir": working_dir,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + DEFAULT_JOB_RETENTION_SECONDS,
            "output": "",
            "is_error": False,
        }
    return job_id


def update_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def cleanup_expired_jobs() -> None:
    now = time.time()
    with JOBS_LOCK:
        expired_ids = [job_id for job_id, job in JOBS.items() if job.get("expires_at", now) < now]
        for job_id in expired_ids:
            JOBS.pop(job_id, None)


def build_job_status_text(job: dict) -> str:
    lines = [
        f"job_id: {job['job_id']}",
        f"tool: {job['tool']}",
        f"status: {job['status']}",
        f"working_dir: {job['working_dir']}",
    ]
    attempts = job.get("attempts") or []
    if attempts:
        lines.append("attempts:")
        for attempt in attempts:
            line = f"- {attempt.get('tool', 'unknown')}: {attempt.get('status', 'unknown')}"
            reason = (attempt.get("reason") or "").strip()
            if reason:
                line += f" ({reason})"
            lines.append(line)
    output = (job.get("output") or "").strip()
    if output:
        lines.extend(["output:", output])
    return "\n".join(lines)


def start_background_job(
    tool_name: str,
    task: str,
    working_dir: str,
    runner,
) -> str:
    job_id = create_job(tool_name, task, working_dir)

    def _worker() -> None:
        update_job(job_id, status="running")
        try:
            result = runner(task, working_dir)
            attempts = None
            if isinstance(result, tuple) and len(result) == 3:
                output, is_error, attempts = result
            else:
                output, is_error = result

            fields = {
                "status": "failed" if is_error else "succeeded",
                "output": output,
                "is_error": is_error,
            }
            if attempts is not None:
                fields["attempts"] = attempts
            update_job(job_id, **fields)
        except Exception as exc:
            update_job(
                job_id,
                status="failed",
                output=f"{tool_name} background job failed: {exc}",
                is_error=True,
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return job_id


def maybe_start_async_job(req_id, arguments: dict, tool_name: str, runner):
    task = arguments.get("task", "").strip()
    working_dir = arguments.get("working_dir", CODEX_DEFAULT_WORKDIR)

    if not task:
        return None, make_response(req_id, make_tool_text_response("Error: task is required", is_error=True))

    if arguments.get("async") is not True:
        return (task, working_dir), None

    job_id = start_background_job(tool_name, task, working_dir, runner)
    log(f"{tool_name}: started background job job_id={job_id} workdir={working_dir!r}")
    return None, make_response(
        req_id,
        make_tool_text_response(
            "\n".join([
                f"{tool_name} started in background.",
                f"JOB_ID={job_id}",
                f"job_id: {job_id}",
                "Use agent_job_status with this job_id to check progress and fetch the final output.",
            ])
        ),
    )


def run_codex_task(task: str, working_dir: str) -> tuple[str, bool]:
    log(f"codex_agent: task={task!r} workdir={working_dir!r}")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="codex_out_")
    os.close(tmp_fd)

    try:
        result = run_agent_command(
            [
                "cmd.exe",
                "/c",
                CODEX_CMD,
                "exec",
                "--full-auto",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C", working_dir,
                "-o", tmp_path,
                task,
            ],
            cwd=working_dir,
        )
        log(f"codex_agent: exit_code={result.returncode}")

        output = ""
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                output = f.read().strip()
        except Exception:
            pass

        return finalize_agent_output(
            result,
            stdout_text=output,
            fallback_label="Codex",
        )
    except subprocess.TimeoutExpired:
        return f"codex_agent timed out after {AGENT_TIMEOUT_SECONDS} seconds", True
    except Exception as exc:
        return f"Failed to run Codex: {exc}", True
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def run_gemini_task(task: str, working_dir: str) -> tuple[str, bool]:
    log(f"gemini_agent: task={task!r} workdir={working_dir!r}")

    try:
        result = run_agent_command(
            ["cmd.exe", "/c", GEMINI_CMD, "-p", task],
            cwd=working_dir,
        )
        log(f"gemini_agent: exit_code={result.returncode}")

        return finalize_agent_output(
            result,
            fallback_label="Gemini",
        )
    except subprocess.TimeoutExpired:
        return f"gemini_agent timed out after {AGENT_TIMEOUT_SECONDS} seconds", True
    except FileNotFoundError:
        return f"Error: gemini command not found at {GEMINI_CMD}", True
    except Exception as exc:
        return f"Failed to run Gemini: {exc}", True


def run_claude_code_task(task: str, working_dir: str) -> tuple[str, bool]:
    log(f"claude_code_agent: task={task!r} workdir={working_dir!r}")

    try:
        result = run_agent_command(
            ["cmd.exe", "/c", CLAUDE_CMD, "-p", task, "--output-format", "text"],
            cwd=working_dir,
        )
        log(f"claude_code_agent: exit_code={result.returncode}")

        return finalize_agent_output(
            result,
            fallback_label="Claude Code",
        )
    except subprocess.TimeoutExpired:
        return f"claude_code_agent timed out after {AGENT_TIMEOUT_SECONDS} seconds", True
    except FileNotFoundError:
        return f"Error: claude command not found at {CLAUDE_CMD}", True
    except Exception as exc:
        return f"Failed to run Claude Code: {exc}", True


def summarize_error_reason(output: str) -> str:
    lowered = (output or "").lower()
    if "quota exceeded" in lowered or "terminalquotaerror" in lowered or "retry in" in lowered:
        return "quota_exceeded"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "429" in lowered:
        return "rate_limited"
    if "connection aborted" in lowered or "context canceled" in lowered:
        return "connection_aborted"
    if "internal error" in lowered or "unexpected critical error" in lowered:
        return "upstream_error"
    return "error"


def should_fallback(tool_name: str, output: str, is_error: bool) -> bool:
    if not is_error:
        return False
    reason = summarize_error_reason(output)
    if tool_name == "gemini_agent":
        return reason in {"quota_exceeded", "timeout", "rate_limited", "connection_aborted", "upstream_error"}
    if tool_name == "codex_agent":
        return reason in {"timeout", "connection_aborted", "upstream_error"}
    return False


def run_smart_agent(task: str, working_dir: str) -> tuple[str, bool, list[dict]]:
    attempts = []
    runners = [
        ("gemini_agent", run_gemini_task),
        ("codex_agent", run_codex_task),
        ("claude_code_agent", run_claude_code_task),
    ]

    for tool_name, runner in runners:
        output, is_error = runner(task, working_dir)
        reason = "" if not is_error else summarize_error_reason(output)
        attempts.append({
            "tool": tool_name,
            "status": "failed" if is_error else "succeeded",
            "reason": reason,
        })
        if not is_error:
            return output, False, attempts
        if not should_fallback(tool_name, output, is_error):
            return output, True, attempts

    return output, True, attempts


# ─── Request Handlers（與 stdio 版邏輯相同，改為 return 而非 send）────────────

def handle_initialize(req_id, params: dict) -> dict:
    client_version = params.get("protocolVersion", PROTOCOL_VERSION)
    log(f"initialize: client protocolVersion={client_version}")
    return make_response(req_id, {
        "protocolVersion": client_version,
        "capabilities": {"tools": {}},
        "serverInfo": SERVER_INFO,
    })


def handle_ping(req_id, params: dict) -> dict:
    log("ping")
    return make_response(req_id, {})


def handle_tools_list(req_id, params: dict) -> dict:
    log(f"tools/list: returning {len(TOOLS)} tool(s)")
    return make_response(req_id, {"tools": TOOLS})


def handle_tools_call(req_id, params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments", {})
    cleanup_expired_jobs()
    log(f"tools/call: name={name} arguments={arguments}")

    if name == "echo":
        message = arguments.get("message", "")
        return make_response(req_id, make_tool_text_response(f"echo: {message}"))

    if name == "codex_agent":
        return handle_codex_agent(req_id, arguments)

    if name == "gemini_agent":
        return handle_gemini_agent(req_id, arguments)

    if name == "claude_code_agent":
        return handle_claude_code_agent(req_id, arguments)

    if name == "agent_job_status":
        return handle_agent_job_status(req_id, arguments)

    if name == "smart_agent":
        return handle_smart_agent(req_id, arguments)

    return make_response(req_id, make_tool_text_response(f"Unknown tool: {name}", is_error=True))


def handle_codex_agent(req_id, arguments: dict) -> dict:
    sync_args, async_response = maybe_start_async_job(req_id, arguments, "codex_agent", run_codex_task)
    if async_response is not None:
        return async_response

    task, working_dir = sync_args
    output, is_error = run_codex_task(task, working_dir)
    return make_response(req_id, make_tool_text_response(output, is_error=is_error))


def handle_gemini_agent(req_id, arguments: dict) -> dict:
    sync_args, async_response = maybe_start_async_job(req_id, arguments, "gemini_agent", run_gemini_task)
    if async_response is not None:
        return async_response

    task, working_dir = sync_args
    output, is_error = run_gemini_task(task, working_dir)
    return make_response(req_id, make_tool_text_response(output, is_error=is_error))


def handle_claude_code_agent(req_id, arguments: dict) -> dict:
    sync_args, async_response = maybe_start_async_job(req_id, arguments, "claude_code_agent", run_claude_code_task)
    if async_response is not None:
        return async_response

    task, working_dir = sync_args
    output, is_error = run_claude_code_task(task, working_dir)
    return make_response(req_id, make_tool_text_response(output, is_error=is_error))


def handle_agent_job_status(req_id, arguments: dict) -> dict:
    job_id = arguments.get("job_id", "").strip()
    if not job_id:
        return make_response(req_id, make_tool_text_response("Error: job_id is required", is_error=True))

    cleanup_expired_jobs()
    job = get_job(job_id)
    if job is None:
        return make_response(req_id, make_tool_text_response(f"Unknown or expired job_id: {job_id}", is_error=True))

    text = build_job_status_text(job)
    is_error = bool(job.get("is_error")) and job.get("status") == "failed"
    return make_response(req_id, make_tool_text_response(text, is_error=is_error))


def handle_smart_agent(req_id, arguments: dict) -> dict:
    sync_args, async_response = maybe_start_async_job(req_id, arguments, "smart_agent", run_smart_agent)
    if async_response is not None:
        return async_response

    task, working_dir = sync_args
    output, is_error, attempts = run_smart_agent(task, working_dir)
    text = output
    if attempts:
        attempt_lines = ["attempts:"]
        for attempt in attempts:
            line = f"- {attempt.get('tool', 'unknown')}: {attempt.get('status', 'unknown')}"
            reason = (attempt.get("reason") or "").strip()
            if reason:
                line += f" ({reason})"
            attempt_lines.append(line)
        text = "\n".join(attempt_lines + ["", output])
    return make_response(req_id, make_tool_text_response(text, is_error=is_error))


REQUEST_HANDLERS = {
    "initialize":  handle_initialize,
    "ping":        handle_ping,
    "tools/list":  handle_tools_list,
    "tools/call":  handle_tools_call,
}


def dispatch(msg: dict):
    """處理單一 JSON-RPC 訊息。Notification 回傳 None；Request 回傳 response dict。"""
    method = msg.get("method", "")
    req_id = msg.get("id")          # Notification 沒有 id
    params = msg.get("params") or {}

    if req_id is None:
        log(f"NOTIFICATION {method} (no response)")
        return None

    handler = REQUEST_HANDLERS.get(method)
    if handler is None:
        log(f"METHOD NOT FOUND: {method}")
        return make_error(req_id, -32601, f"Method not found: {method}")

    try:
        return handler(req_id, params)
    except Exception as exc:
        log(f"HANDLER ERROR [{method}]: {exc}")
        return make_error(req_id, -32603, f"Internal error: {exc}")


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class MCPHTTPHandler(BaseHTTPRequestHandler):

    # ── GET（探索 / SSE 健康檢查）────────────────────────────────────────────
    def do_GET(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "server": SERVER_INFO,
            "protocolVersion": PROTOCOL_VERSION,
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ── CORS preflight ────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self._add_cors_headers()
        self.end_headers()

    # ── 主要端點 ──────────────────────────────────────────────────────────────
    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return

        # ── Bearer token 驗證 ─────────────────────────────────────────────────
        auth = self.headers.get("Authorization", "")
        if auth:
            token = auth.removeprefix("Bearer ").strip()
            if token != API_TOKEN:
                log(f"401 Unauthorized: invalid token")
                self.send_response(401)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Unauthorized")
                return

        # ── Origin 驗證（spec 強制，防 DNS rebinding）─────────────────────────
        origin = self.headers.get("Origin", "")
        if origin and not self._is_allowed_origin(origin):
            log(f"403 Forbidden: Origin={origin!r}")
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Forbidden: Origin not allowed")
            return

        # ── 讀取 body ─────────────────────────────────────────────────────────
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        log(f"RECV ← {raw.decode('utf-8', errors='replace')}")

        # ── JSON parse ────────────────────────────────────────────────────────
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(make_error(None, -32700, f"Parse error: {exc}"), status=400)
            return

        if not isinstance(msg, dict):
            self._send_json(make_error(None, -32600, "Invalid Request: expected JSON object"), status=400)
            return

        # ── Dispatch ──────────────────────────────────────────────────────────
        response = dispatch(msg)

        if response is None:
            # Notification → 202 Accepted, 不回 body
            self.send_response(202)
            self._add_cors_headers()
            self.end_headers()
            return

        self._send_json(response)

    # ── 回應輔助 ──────────────────────────────────────────────────────────────
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        log(f"SEND → {body.decode('utf-8')}")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _add_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Accept, Mcp-Session-Id")

    def _is_allowed_origin(self, origin: str) -> bool:
        try:
            hostname = urllib.parse.urlparse(origin).hostname or ""
            return hostname in ALLOWED_HOSTNAMES
        except Exception:
            return False

    # ── 把 http.server 的 access log 導到 stderr ──────────────────────────────
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} - {fmt % args}")


# ─── 主程式 ───────────────────────────────────────────────────────────────────

def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MCPHTTPHandler)
    log(f"handcraft-mcp HTTP server starting")
    log(f"Protocol : {PROTOCOL_VERSION}")
    log(f"Endpoint : POST http://localhost:{PORT}/mcp")
    log(f"Allowed origins: {ALLOWED_HOSTNAMES}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Server stopped (KeyboardInterrupt)")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
