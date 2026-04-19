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
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from mmx_handlers import DISPATCH, hmi, hmvd, hms, hmu, hmv, hmsq, hmc, hmq

# ── mmx handler aliases（對應 dispatch 的完整名稱）─────────────────────────────
handle_mmx_image_generate   = hmi
handle_mmx_video_generate   = hmv
handle_mmx_speech_synthesize = hms
handle_mmx_music_generate   = hmu
handle_mmx_vision_describe  = hmvd
handle_mmx_search_query     = hmsq
handle_mmx_text_chat        = hmc
handle_mmx_quota_show       = hmq

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ── Secrets（由 Doppler 注入）─────────────────────────────────────────────────
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
API_TOKEN = os.getenv("MCP_API_TOKEN", "")

CODEX_CMD = r"C:\Users\EdgarsTool\AppData\Roaming\npm\codex.cmd"
CLAUDE_CMD = shutil.which("claude") or "claude"
GEMINI_CMD = shutil.which("gemini") or "gemini"
OLLAMA_CMD = r"C:\Users\EdgarsTool\AppData\Local\Programs\Ollama\ollama.exe"
CODEX_DEFAULT_WORKDIR = r"C:\Users\EdgarsTool"
AGENT_TIMEOUT_SECONDS = int(os.getenv("MCP_AGENT_TIMEOUT_SECONDS", "300"))

PORT = 8765
PROTOCOL_VERSION = "2025-11-25"
DEFAULT_JOB_RETENTION_SECONDS = int(os.getenv("MCP_JOB_RETENTION_SECONDS", "3600"))

SERVER_INFO = {
    "name": "handcraft-mcp",
    "version": "0.1.0",
}

JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}

# ── OAuth 2.0 一次性授權碼（記憶體暫存，重啟後清空）──────────────────────────
OAUTH_CODES_LOCK = threading.Lock()
OAUTH_CODES: dict[str, dict] = {}
BASE_URL = os.getenv("MCP_BASE_URL", "https://mcp.whoasked.vip")

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
    {
        "name": "notion_search",
        "description": (
            "Search pages and databases in Notion. Returns a list of matching pages "
            "with their titles, IDs, and URLs. Use this to find Notion content by keyword."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keyword to find in Notion pages and databases.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of results to return (default 10, max 20).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "notion_get_page",
        "description": (
            "Fetch the content of a specific Notion page by its page ID or URL. "
            "Returns the page title and all text blocks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "Notion page ID (UUID format) or full Notion page URL.",
                },
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "mmx_image_generate",
        "description": (
            "Generate images using MiniMax AI image-01 model. "
            "Returns image URLs or saved file paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image description prompt."},
                "aspect_ratio": {"type": "string", "description": "Aspect ratio like 16:9, 1:1, 9:16."},
                "n": {"type": "integer", "description": "Number of images to generate (default 1)."},
                "out_dir": {"type": "string", "description": "Directory to save images."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "mmx_video_generate",
        "description": (
            "Generate videos using MiniMax AI Hailuo-2.3 model. "
            "This is async — set async=true to get a job_id immediately, "
            "or wait for the video to be generated and returned as a file path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Video description prompt."},
                "async": {"type": "boolean", "description": "Return job_id immediately without waiting."},
                "first_frame": {"type": "string", "description": "Path or URL to first frame image."},
                "download": {"type": "string", "description": "File path to save the video."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "mmx_speech_synthesize",
        "description": (
            "Text-to-speech using MiniMax speech-2.8-hd model. "
            "Converts text to audio file (mp3 by default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to synthesize (max 10k chars)."},
                "text_file": {"type": "string", "description": "Path to text file (use - for stdin)."},
                "voice": {"type": "string", "description": "Voice ID (default: English_expressive_narrator)."},
                "model": {"type": "string", "description": "Model: speech-2.8-hd, speech-2.6, or speech-02."},
                "speed": {"type": "number", "description": "Speed multiplier."},
                "format": {"type": "string", "description": "Audio format (default: mp3)."},
                "out": {"type": "string", "description": "Output file path."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "mmx_music_generate",
        "description": (
            "Generate music using MiniMax music-2.5 model. "
            "Can create songs with vocals or instrumental music."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Music style/description prompt."},
                "lyrics": {"type": "string", "description": "Song lyrics with structure tags."},
                "vocals": {"type": "string", "description": "Vocal style description."},
                "genre": {"type": "string", "description": "Music genre."},
                "mood": {"type": "string", "description": "Mood or emotion."},
                "instruments": {"type": "string", "description": "Instruments to feature."},
                "bpm": {"type": "number", "description": "Exact tempo in BPM."},
                "instrumental": {"type": "boolean", "description": "Generate instrumental without vocals."},
                "out": {"type": "string", "description": "Output file path."},
            },
        },
    },
    {
        "name": "mmx_vision_describe",
        "description": (
            "Image understanding via MiniMax VL model. "
            "Describes or answers questions about an image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Image path or URL."},
                "file_id": {"type": "string", "description": "Pre-uploaded file ID."},
                "prompt": {"type": "string", "description": "Question about the image."},
            },
            "required": ["image"],
        },
    },
    {
        "name": "mmx_search_query",
        "description": "Web search via MiniMax AI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query."},
            },
            "required": ["q"],
        },
    },
    {
        "name": "mmx_text_chat",
        "description": (
            "Chat completion using MiniMax MiniMax-M2.7 model. "
            "Supports multi-turn conversation and system prompts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message text. Prefix with role: to set role."},
                "system": {"type": "string", "description": "System prompt."},
                "model": {"type": "string", "description": "Model ID (default: MiniMax-M2.7)."},
                "max_tokens": {"type": "integer", "description": "Max tokens (default: 4096)."},
                "temperature": {"type": "number", "description": "Sampling temperature (0.0-1.0)."},
            },
            "required": ["message"],
        },
    },
    {
        "name": "mmx_quota_show",
        "description": "Display MiniMax Token Plan usage and remaining quotas.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ollama_agent",
        "description": (
            "Delegates a task to a local Ollama AI model (qwen3.5). "
            "Fast, runs locally, no API cost. "
            "Use for quick coding tasks, summarization, and general automation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task or instruction for Ollama to execute."},
                "model": {"type": "string", "description": "Model name (default: qwen3.5:latest)."},
                "working_dir": {"type": "string", "description": f"Working directory (default: {CODEX_DEFAULT_WORKDIR})"},
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

    if name == "notion_search":
        return handle_notion_search(req_id, arguments)

    if name == "notion_get_page":
        return handle_notion_get_page(req_id, arguments)

    if name == "mmx_image_generate":
        return handle_mmx_image_generate(req_id, arguments)
    if name == "mmx_video_generate":
        return handle_mmx_video_generate(req_id, arguments)
    if name == "mmx_speech_synthesize":
        return handle_mmx_speech_synthesize(req_id, arguments)
    if name == "mmx_music_generate":
        return handle_mmx_music_generate(req_id, arguments)
    if name == "mmx_vision_describe":
        return handle_mmx_vision_describe(req_id, arguments)
    if name == "mmx_search_query":
        return handle_mmx_search_query(req_id, arguments)
    if name == "mmx_text_chat":
        return handle_mmx_text_chat(req_id, arguments)
    if name == "mmx_quota_show":
        return handle_mmx_quota_show(req_id, arguments)

    if name == "ollama_agent":
        return handle_ollama_agent(req_id, arguments)

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


# ── Notion helpers ────────────────────────────────────────────────────────────

def _notion_request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """發送 Notion API 請求，回傳 parsed JSON 或拋出 Exception。"""
    if not NOTION_API_KEY:
        raise ValueError("NOTION_API_KEY not set — add it in Doppler and restart server")
    url = "https://api.notion.com/v1" + path
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {NOTION_API_KEY}")
    req.add_header("Notion-Version", "2022-06-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_plain_text(rich_text_list: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text_list)


def _page_title(page: dict) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return _extract_plain_text(prop.get("title", []))
    return "(no title)"


def _blocks_to_text(blocks: list) -> str:
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = _extract_plain_text(rich).strip()
        if text:
            if btype.startswith("heading"):
                lines.append(f"\n## {text}")
            elif btype == "bulleted_list_item":
                lines.append(f"- {text}")
            elif btype == "numbered_list_item":
                lines.append(f"1. {text}")
            elif btype == "to_do":
                checked = "x" if content.get("checked") else " "
                lines.append(f"[{checked}] {text}")
            elif btype == "code":
                lang = content.get("language", "")
                lines.append(f"```{lang}\n{text}\n```")
            else:
                lines.append(text)
    return "\n".join(lines)


def handle_notion_search(req_id, arguments: dict) -> dict:
    query = arguments.get("query", "").strip()
    limit = min(int(arguments.get("limit", 10)), 20)
    if not query:
        return make_response(req_id, make_tool_text_response("Error: query is required", is_error=True))
    try:
        data = _notion_request("/search", "POST", {"query": query, "page_size": limit})
        results = data.get("results", [])
        if not results:
            return make_response(req_id, make_tool_text_response(f"No results found for: {query}"))
        lines = [f"Found {len(results)} result(s) for \"{query}\":\n"]
        for item in results:
            obj_type = item.get("object", "")
            title = _page_title(item) if obj_type == "page" else item.get("title", "(no title)")
            url = item.get("url", "")
            page_id = item.get("id", "")
            lines.append(f"- [{title}]({url})\n  id: {page_id}  type: {obj_type}")
        return make_response(req_id, make_tool_text_response("\n".join(lines)))
    except Exception as exc:
        return make_response(req_id, make_tool_text_response(f"Notion search error: {exc}", is_error=True))


def handle_notion_get_page(req_id, arguments: dict) -> dict:
    page_id = arguments.get("page_id", "").strip()
    if not page_id:
        return make_response(req_id, make_tool_text_response("Error: page_id is required", is_error=True))
    # 從 URL 取出 ID（最後一段 32 碼 hex，去掉 dash）
    if page_id.startswith("http"):
        raw = page_id.rstrip("/").split("/")[-1].split("?")[0]
        page_id = raw[-32:].replace("-", "")
        page_id = f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
    try:
        page = _notion_request(f"/pages/{page_id}")
        title = _page_title(page)
        url = page.get("url", "")
        blocks_data = _notion_request(f"/blocks/{page_id}/children?page_size=100")
        blocks = blocks_data.get("results", [])
        body = _blocks_to_text(blocks) or "(no content)"
        text = f"# {title}\n{url}\n\n{body}"
        return make_response(req_id, make_tool_text_response(text))
    except Exception as exc:
        return make_response(req_id, make_tool_text_response(f"Notion get page error: {exc}", is_error=True))


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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/.well-known/oauth-authorization-server":
            self._handle_oauth_metadata()
        elif path == "/.well-known/oauth-protected-resource":
            self._handle_resource_metadata()
        elif path == "/authorize":
            self._handle_authorize(parsed.query)
        elif path == "/mcp":
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
        else:
            self.send_response(404)
            self.end_headers()

    # ── CORS preflight ────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self._add_cors_headers()
        self.end_headers()

    # ── OAuth 2.0 helpers ────────────────────────────────────────────────────
    def _send_oauth_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._add_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_oauth_metadata(self) -> None:
        self._send_oauth_json({
            "issuer": BASE_URL,
            "authorization_endpoint": f"{BASE_URL}/authorize",
            "token_endpoint": f"{BASE_URL}/token",
            "registration_endpoint": f"{BASE_URL}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    def _handle_resource_metadata(self) -> None:
        self._send_oauth_json({
            "resource": BASE_URL,
            "authorization_servers": [BASE_URL],
        })

    def _handle_authorize(self, query_string: str) -> None:
        params = urllib.parse.parse_qs(query_string)
        redirect_uri = params.get("redirect_uri", [""])[0]
        state = params.get("state", [""])[0]
        if not redirect_uri:
            self.send_response(400)
            self.end_headers()
            return
        code = str(uuid.uuid4())
        code_verifier_challenge = params.get("code_challenge", [""])[0]
        with OAUTH_CODES_LOCK:
            OAUTH_CODES[code] = {
                "created_at": time.time(),
                "used": False,
                "code_challenge": code_verifier_challenge,
                "redirect_uri": redirect_uri,
            }
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}"
        if state:
            location += f"&state={urllib.parse.quote(state)}"
        log(f"OAuth /authorize → redirect to {redirect_uri[:60]}...")
        self.send_response(302)
        self.send_header("Location", location)
        self._add_cors_headers()
        self.end_headers()

    def _handle_token(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        ct = self.headers.get("Content-Type", "")
        if "application/json" in ct:
            try:
                params: dict = json.loads(raw.decode("utf-8"))
            except Exception:
                params = {}
        else:
            params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()}

        grant_type = params.get("grant_type", "")
        if grant_type != "authorization_code":
            self._send_oauth_json({"error": "unsupported_grant_type"}, 400)
            return

        code = params.get("code", "")
        with OAUTH_CODES_LOCK:
            entry = OAUTH_CODES.get(code)
            if not entry or entry.get("used"):
                self._send_oauth_json({"error": "invalid_grant"}, 400)
                return
            if time.time() - entry["created_at"] > 600:  # 10 分鐘過期
                self._send_oauth_json({"error": "invalid_grant", "error_description": "code expired"}, 400)
                return
            entry["used"] = True

        access_token = API_TOKEN if API_TOKEN else "handcraft-dev-token"
        log("OAuth /token → issued access_token")
        self._send_oauth_json({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 7776000,  # 90 天
        })

    def _handle_register(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            meta: dict = json.loads(raw.decode("utf-8"))
        except Exception:
            meta = {}
        self._send_oauth_json({
            "client_id": str(uuid.uuid4()),
            "client_secret_expires_at": 0,
            "redirect_uris": meta.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }, 201)

    # ── 主要端點 ──────────────────────────────────────────────────────────────
    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path == "/token":
            self._handle_token()
            return
        if parsed_path == "/register":
            self._handle_register()
            return
        if parsed_path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return

        # ── Bearer token 驗證 ─────────────────────────────────────────────────
        auth = self.headers.get("Authorization", "")
        if API_TOKEN:  # Token 已設定時，header 必須存在且正確
            if not auth:
                log("401 Unauthorized: missing token")
                self.send_response(401)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Unauthorized")
                return
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


def run_ollama_task(task: str, model: str, working_dir: str) -> tuple[str, bool]:
    log(f"ollama_agent: task={task!r} model={model!r} workdir={working_dir!r}")
    try:
        result = run_agent_command(
            ["cmd.exe", "/c", OLLAMA_CMD, "run", model, task],
            cwd=working_dir,
        )
        log(f"ollama_agent: exit_code={result.returncode}")
        return finalize_agent_output(result, fallback_label="Ollama")
    except subprocess.TimeoutExpired:
        return f"ollama_agent timed out after {AGENT_TIMEOUT_SECONDS} seconds", True
    except FileNotFoundError:
        return f"Error: ollama command not found at {OLLAMA_CMD}", True
    except Exception as exc:
        return f"Failed to run Ollama: {exc}", True


def handle_ollama_agent(req_id, arguments: dict) -> dict:
    sync_args, async_response = maybe_start_async_job(req_id, arguments, "ollama_agent",
        lambda: run_ollama_task(
            arguments["task"],
            arguments.get("model", "qwen3.5:latest"),
            arguments.get("working_dir", CODEX_DEFAULT_WORKDIR),
        )
    )
    if async_response is not None:
        return async_response
    output, is_error = sync_args
    return make_response(req_id, make_tool_text_response(output, is_error=is_error))


if __name__ == "__main__":
    main()
