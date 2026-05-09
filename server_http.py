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
import fnmatch
import platform
import re
import string
import datetime
from pathlib import Path
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
NOTION_API_KEY      = os.getenv("NOTION_API_KEY", "")
API_TOKEN           = os.getenv("MCP_API_TOKEN", "")
PERPLEXITY_API_KEY  = os.getenv("PERPLEXITY_API_KEY", "")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
LINEAR_API_KEY      = os.getenv("LINEAR_API_KEY", "")

SCREENSHOTS_DIR = Path(r"C:\Users\EdgarsTool\Projects\mcp-handcraft\.screenshots")
VAULT_ROOT      = Path(r"D:\Edgar'sObsidianVault")

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
        "name": "agent_job_list",
        "description": (
            "Lists recent background agent jobs. "
            "Supports optional status filter and limit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter: queued, running, succeeded, failed",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max jobs to return (default 20, max 100).",
                },
            },
        },
    },
    {
        "name": "agent_job_cleanup",
        "description": (
            "Deletes expired background agent jobs from in-memory storage "
            "and returns how many records were removed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
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
    # ── 檔案系統工具 ──────────────────────────────────────────────────────────
    {
        "name": "fs_list",
        "description": "List directory contents with file sizes, types, and modification dates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list"},
                "show_hidden": {"type": "boolean", "description": "Include hidden files/folders (default: false)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "fs_read",
        "description": "Read the contents of a file. Truncates at max_lines to avoid overloading context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "max_lines": {"type": "integer", "description": "Max lines to return (default: 200)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "fs_write",
        "description": "Write or create a file. Can append to existing file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {"type": "boolean", "description": "Append instead of overwrite (default: false)"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "fs_move",
        "description": "Move or rename a file or folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source path"},
                "dst": {"type": "string", "description": "Destination path"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "fs_delete",
        "description": "Safely delete a file or folder by moving it to a trash folder (C:\\Users\\EdgarsTool\\.mcp-trash). NOT permanent — can be recovered manually.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or folder path to delete (moved to trash)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "fs_search",
        "description": "Search for files by name pattern (glob) and optionally by content substring.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Root directory to search in"},
                "pattern": {"type": "string", "description": "Filename glob pattern (e.g. '*.py', '*.md', default: '*')"},
                "search_content": {"type": "string", "description": "Optional substring to search inside file contents"},
                "max_results": {"type": "integer", "description": "Max results to return (default: 50)"},
            },
            "required": ["directory"],
        },
    },
    {
        "name": "fs_disk_info",
        "description": "Show disk usage for all drives (used/free/total space with visual bar).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ── 系統工具 ──────────────────────────────────────────────────────────────
    {
        "name": "sys_run",
        "description": "Run a PowerShell command on the local machine and return output. Timeout max 120s. Dangerous patterns are blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell command to execute"},
                "working_dir": {"type": "string", "description": "Working directory (default: user home)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30, max: 120)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "sys_info",
        "description": "Get system information: CPU, RAM usage, OS version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sys_processes",
        "description": "List running processes sorted by memory or CPU usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of processes to return (default: 20)"},
                "sort_by": {"type": "string", "description": "Sort by: 'memory' (default), 'cpu', or 'name'"},
            },
        },
    },
    # ── Git 工具 ─────────────────────────────────────────────────────────────
    {
        "name": "git_status",
        "description": "Show git working tree status for a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": f"Repo path (default: {CODEX_DEFAULT_WORKDIR})"},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Show recent git commit history (one-line format).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repo path"},
                "limit": {"type": "integer", "description": "Number of commits (default: 10)"},
            },
        },
    },
    {
        "name": "git_diff",
        "description": "Show git diff summary (changed files and line counts).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repo path"},
                "staged": {"type": "boolean", "description": "Show staged diff (default: false = unstaged)"},
            },
        },
    },
    {
        "name": "git_commit",
        "description": "Stage files and create a git commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repo path"},
                "message": {"type": "string", "description": "Commit message"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage (empty = git add -A for all changes)",
                },
            },
            "required": ["message"],
        },
    },
    # ── Playwright 瀏覽器工具 ─────────────────────────────────────────────────
    {
        "name": "browser_screenshot",
        "description": "Open a URL in headless Chromium, wait for load, save a screenshot PNG, and return the file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
                "wait_ms": {"type": "integer", "description": "Extra wait after load in ms (default: 2000)"},
                "full_page": {"type": "boolean", "description": "Capture full page scroll height (default: false)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_get_text",
        "description": "Fetch a URL in headless Chromium and return the visible text content of the page (or a CSS selector).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
                "selector": {"type": "string", "description": "CSS selector to extract text from (default: body)"},
                "wait_ms": {"type": "integer", "description": "Extra wait after load in ms (default: 1000)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_run_script",
        "description": "Navigate to a URL and run JavaScript, returning the result. Useful for scraping or checking page state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
                "script": {"type": "string", "description": "JavaScript to evaluate (return value is serialized to JSON)"},
                "wait_ms": {"type": "integer", "description": "Extra wait after load (default: 1000)"},
            },
            "required": ["url", "script"],
        },
    },
    # ── Obsidian Vault 工具 ───────────────────────────────────────────────────
    {
        "name": "vault_read",
        "description": "Read an Obsidian note by relative path (e.g. '00 Inbox/my-note.md').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside vault"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "vault_write",
        "description": "Create or overwrite an Obsidian note. Creates parent folders automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path inside vault"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "vault_append",
        "description": "Append text to an existing Obsidian note (adds a newline before appended content).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path inside vault"},
                "content": {"type": "string", "description": "Text to append"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "vault_list",
        "description": "List files and folders inside a vault directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative folder path (default: vault root)"},
            },
        },
    },
    {
        "name": "vault_search",
        "description": "Full-text search across all .md files in the vault. Returns matching file paths and context snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string", "description": "Text to search for"},
                "max_results": {"type": "integer", "description": "Max results (default: 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_delete",
        "description": "Delete a vault note (moves to vault .trash folder, recoverable from Obsidian).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside vault"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "vault_move",
        "description": "Move or rename a vault note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Source relative path"},
                "dst": {"type": "string", "description": "Destination relative path"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "vault_daily_note",
        "description": "Get or create today's daily note in 00 Inbox/Daily/YYYY-MM-DD.md with the Daily Notes template.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format (default: today)"},
            },
        },
    },
    {
        "name": "vault_recent",
        "description": "List the most recently modified notes in the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit":  {"type": "integer", "description": "Number of notes (default: 15)"},
                "folder": {"type": "string",  "description": "Limit to a subfolder (optional)"},
            },
        },
    },
    {
        "name": "vault_tasks",
        "description": "Find all unchecked tasks (- [ ]) across the vault. Useful for a global TODO overview.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Limit search to a subfolder (optional)"},
                "limit":  {"type": "integer", "description": "Max tasks to return (default: 50)"},
            },
        },
    },
    {
        "name": "vault_tags",
        "description": "List all tags used across the vault with their counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vault_create_from_template",
        "description": "Create a new note from a vault template. Available templates: Daily Notes, Project, Learning Project, Research Clipping, Service Subscription, Meeting Notes, Weekly Review, Decision Record.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "description": "Template name (e.g. 'Project', 'Meeting Notes')"},
                "title":    {"type": "string", "description": "Note title (used as filename and in content)"},
                "folder":   {"type": "string", "description": "Destination folder (default: 00 Inbox)"},
                "fields":   {"type": "object", "description": "Key-value pairs to fill in template variables"},
            },
            "required": ["template", "title"],
        },
    },
    {
        "name": "vault_sort_inbox",
        "description": (
            "自動掃描 Obsidian Vault 的 00 Inbox，判斷每個散落筆記的分類，"
            "批次搬移到正確的 PARA 資料夾（01 Projects / 02 Areas / 03 Resources / 04 Archive）。"
            "不會動 Daily Notes 子資料夾和 Don't Touch 子資料夾。"
            "完成後回傳搬移清單。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "若為 true，只列出分類結果但不實際搬移（預設 false）",
                },
            },
        },
    },
    # ── 免費圖片生成（Pollinations.AI，不需 API key）─────────────────────────
    {
        "name": "image_generate_free",
        "description": (
            "Generate an image for FREE using Pollinations.AI (no API key needed). "
            "Saves PNG to .screenshots/ and returns the file path. "
            "Models: flux (default, best quality), turbo (fast), gptimage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image description prompt"},
                "width":  {"type": "integer", "description": "Width in px (default: 1024)"},
                "height": {"type": "integer", "description": "Height in px (default: 1024)"},
                "model":  {"type": "string",  "description": "Model: flux (default) | turbo | gptimage"},
                "seed":   {"type": "integer", "description": "Seed for reproducibility (optional)"},
            },
            "required": ["prompt"],
        },
    },
    # ── Web Search ────────────────────────────────────────────────────────────
    {
        "name": "web_search",
        "description": "Search the web using Perplexity AI and return a summarized answer with sources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    # ── Linear 工具 ───────────────────────────────────────────────────────────
    {
        "name": "linear_issues",
        "description": "List Linear issues. Filter by state name (e.g. 'In Progress', 'Todo', 'Done').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "Filter by state name (optional)"},
                "limit": {"type": "integer", "description": "Number of issues (default: 10)"},
                "assignee_me": {"type": "boolean", "description": "Only show issues assigned to me (default: false)"},
            },
        },
    },
    {
        "name": "linear_create_issue",
        "description": "Create a new Linear issue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "description": {"type": "string", "description": "Issue description (markdown)"},
                "team_name": {"type": "string", "description": "Team name (default: first team found)"},
                "priority": {"type": "integer", "description": "Priority 0=none 1=urgent 2=high 3=medium 4=low (default: 3)"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "linear_update_issue",
        "description": "Update a Linear issue state or add a comment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_id": {"type": "string", "description": "Issue ID (e.g. 'WHO-123')"},
                "state": {"type": "string", "description": "New state name (e.g. 'Done', 'In Progress')"},
                "comment": {"type": "string", "description": "Comment to add"},
            },
            "required": ["issue_id"],
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


def require_mcp_api_token() -> str:
    token = os.getenv("MCP_API_TOKEN")
    if token is None or not token.strip():
        log("MCP_API_TOKEN is required and must be a non-empty string. Refusing to start.")
        raise SystemExit(1)
    return token


def make_response(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def make_tool_text_response(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def run_agent_command(
    command: list[str],
    cwd: str,
    *,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=AGENT_TIMEOUT_SECONDS,
        cwd=cwd,
        env=env,
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


def cleanup_expired_jobs() -> int:
    now = time.time()
    with JOBS_LOCK:
        expired_ids = [job_id for job_id, job in JOBS.items() if job.get("expires_at", now) < now]
        for job_id in expired_ids:
            JOBS.pop(job_id, None)
    return len(expired_ids)


def list_jobs(*, status: str | None = None, limit: int = 20) -> list[dict]:
    if limit <= 0:
        limit = 20
    limit = min(limit, 100)

    with JOBS_LOCK:
        jobs = [dict(job) for job in JOBS.values()]

    if status:
        jobs = [job for job in jobs if job.get("status") == status]

    jobs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return jobs[:limit]


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
            # Force Claude Code to use the locally logged-in first-party account.
            # Doppler or shell-level Anthropic API settings can otherwise override
            # OAuth and make claude_code_agent fail with "Invalid API key".
            env_overrides={
                "ANTHROPIC_AUTH_TOKEN": None,
                "ANTHROPIC_API_KEY": None,
                "ANTHROPIC_BASE_URL": None,
                "ANTHROPIC_MODEL": None,
                "ANTHROPIC_SMALL_FAST_MODEL": None,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": None,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": None,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": None,
            },
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

    if name == "agent_job_list":
        return handle_agent_job_list(req_id, arguments)

    if name == "agent_job_cleanup":
        return handle_agent_job_cleanup(req_id, arguments)

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

    # ── 檔案系統
    if name == "fs_list":
        return handle_fs_list(req_id, arguments)
    if name == "fs_read":
        return handle_fs_read(req_id, arguments)
    if name == "fs_write":
        return handle_fs_write(req_id, arguments)
    if name == "fs_move":
        return handle_fs_move(req_id, arguments)
    if name == "fs_delete":
        return handle_fs_delete(req_id, arguments)
    if name == "fs_search":
        return handle_fs_search(req_id, arguments)
    if name == "fs_disk_info":
        return handle_fs_disk_info(req_id, arguments)

    # ── 系統
    if name == "sys_run":
        return handle_sys_run(req_id, arguments)
    if name == "sys_info":
        return handle_sys_info(req_id, arguments)
    if name == "sys_processes":
        return handle_sys_processes(req_id, arguments)

    # ── Git
    if name == "git_status":
        return handle_git_status(req_id, arguments)
    if name == "git_log":
        return handle_git_log(req_id, arguments)
    if name == "git_diff":
        return handle_git_diff(req_id, arguments)
    if name == "git_commit":
        return handle_git_commit(req_id, arguments)

    # ── Playwright
    if name == "browser_screenshot":
        return handle_browser_screenshot(req_id, arguments)
    if name == "browser_get_text":
        return handle_browser_get_text(req_id, arguments)
    if name == "browser_run_script":
        return handle_browser_run_script(req_id, arguments)

    # ── Obsidian
    if name == "vault_read":              return handle_vault_read(req_id, arguments)
    if name == "vault_write":             return handle_vault_write(req_id, arguments)
    if name == "vault_append":            return handle_vault_append(req_id, arguments)
    if name == "vault_list":              return handle_vault_list(req_id, arguments)
    if name == "vault_search":            return handle_vault_search(req_id, arguments)
    if name == "vault_delete":            return handle_vault_delete(req_id, arguments)
    if name == "vault_move":              return handle_vault_move(req_id, arguments)
    if name == "vault_daily_note":        return handle_vault_daily_note(req_id, arguments)
    if name == "vault_recent":            return handle_vault_recent(req_id, arguments)
    if name == "vault_tasks":             return handle_vault_tasks(req_id, arguments)
    if name == "vault_tags":              return handle_vault_tags(req_id, arguments)
    if name == "vault_create_from_template": return handle_vault_create_from_template(req_id, arguments)
    if name == "vault_sort_inbox":           return handle_vault_sort_inbox(req_id, arguments)

    # ── 免費圖片生成
    if name == "image_generate_free":
        return handle_image_generate_free(req_id, arguments)

    # ── Web Search
    if name == "web_search":
        return handle_web_search(req_id, arguments)

    # ── Linear
    if name == "linear_issues":
        return handle_linear_issues(req_id, arguments)
    if name == "linear_create_issue":
        return handle_linear_create_issue(req_id, arguments)
    if name == "linear_update_issue":
        return handle_linear_update_issue(req_id, arguments)

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


def handle_agent_job_list(req_id, arguments: dict) -> dict:
    status = (arguments.get("status") or "").strip()
    limit_raw = arguments.get("limit", 20)

    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return make_response(req_id, make_tool_text_response("Error: limit must be an integer", is_error=True))

    if status and status not in {"queued", "running", "succeeded", "failed"}:
        return make_response(
            req_id,
            make_tool_text_response("Error: status must be one of queued/running/succeeded/failed", is_error=True),
        )

    cleanup_expired_jobs()
    jobs = list_jobs(status=status or None, limit=limit)

    if not jobs:
        filter_text = f" (status={status})" if status else ""
        return make_response(req_id, make_tool_text_response(f"No jobs found{filter_text}."))

    lines = [f"Found {len(jobs)} job(s):"]
    for job in jobs:
        lines.append(
            " | ".join(
                [
                    f"job_id={job.get('job_id', '')}",
                    f"tool={job.get('tool', '')}",
                    f"status={job.get('status', '')}",
                    f"updated_at={job.get('updated_at', 0):.0f}",
                ]
            )
        )

    return make_response(req_id, make_tool_text_response("\n".join(lines)))


def handle_agent_job_cleanup(req_id, arguments: dict) -> dict:  # pylint: disable=unused-argument
    removed = cleanup_expired_jobs()
    return make_response(req_id, make_tool_text_response(f"Expired jobs removed: {removed}"))


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
    global API_TOKEN
    API_TOKEN = require_mcp_api_token()
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
        lambda t, w: run_ollama_task(t, arguments.get("model", "qwen3.5:latest"), w)
    )
    if async_response is not None:
        return async_response
    task, working_dir = sync_args
    output, is_error = run_ollama_task(task, arguments.get("model", "qwen3.5:latest"), working_dir)
    return make_response(req_id, make_tool_text_response(output, is_error=is_error))


# ─── File System Handlers ────────────────────────────────────────────────────

MCP_TRASH_DIR = Path(r"C:\Users\EdgarsTool\.mcp-trash")


def handle_fs_list(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    show_hidden = arguments.get("show_hidden", False)
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = Path(path)
        if not p.exists():
            return make_response(req_id, make_tool_text_response(f"Error: path does not exist: {path}", is_error=True))
        if not p.is_dir():
            return make_response(req_id, make_tool_text_response(f"Error: not a directory: {path}", is_error=True))
        entries = []
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                stat = item.stat()
                mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                kind = "DIR " if item.is_dir() else "FILE"
                size_str = f"{stat.st_size:>12,}" if item.is_file() else "           —"
                entries.append(f"{kind}  {mtime}  {size_str}  {item.name}")
            except (PermissionError, OSError):
                entries.append(f"???   (permission denied)              {item.name}")
        header = f"Directory: {path}\n{len(entries)} item(s)\n" + "─" * 65
        body = "\n".join(entries) if entries else "(empty)"
        return make_response(req_id, make_tool_text_response(f"{header}\n{body}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_read(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    max_lines = int(arguments.get("max_lines", 200))
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = Path(path)
        if not p.exists():
            return make_response(req_id, make_tool_text_response(f"Error: file not found: {path}", is_error=True))
        if not p.is_file():
            return make_response(req_id, make_tool_text_response(f"Error: not a file: {path}", is_error=True))
        size = p.stat().st_size
        if size > 5 * 1024 * 1024:
            return make_response(req_id, make_tool_text_response(
                f"Error: file too large ({size:,} bytes). Max 5MB.", is_error=True
            ))
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total = len(lines)
        if total > max_lines:
            body = "\n".join(lines[:max_lines]) + f"\n... (showing {max_lines}/{total} lines)"
        else:
            body = content
        return make_response(req_id, make_tool_text_response(
            f"File: {path} ({size:,} bytes, {total} lines)\n---\n{body}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_write(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    content = arguments.get("content", "")
    append = arguments.get("append", False)
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended to" if append else "Written"
        size = p.stat().st_size
        return make_response(req_id, make_tool_text_response(f"{action}: {path} ({size:,} bytes)"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_move(req_id, arguments: dict) -> dict:
    src = arguments.get("src", "").strip()
    dst = arguments.get("dst", "").strip()
    if not src or not dst:
        return make_response(req_id, make_tool_text_response("Error: src and dst are required", is_error=True))
    try:
        src_p = Path(src)
        dst_p = Path(dst)
        if not src_p.exists():
            return make_response(req_id, make_tool_text_response(f"Error: source not found: {src}", is_error=True))
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return make_response(req_id, make_tool_text_response(f"Moved: {src} → {dst}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_delete(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = Path(path)
        if not p.exists():
            return make_response(req_id, make_tool_text_response(f"Error: not found: {path}", is_error=True))
        MCP_TRASH_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_dest = MCP_TRASH_DIR / f"{ts}_{p.name}"
        shutil.move(str(p), str(trash_dest))
        return make_response(req_id, make_tool_text_response(
            f"Moved to trash: {path}\nTrash location: {trash_dest}\nTo restore: move it back manually."
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_search(req_id, arguments: dict) -> dict:
    directory = arguments.get("directory", "").strip()
    pattern = arguments.get("pattern", "*").strip()
    search_content = arguments.get("search_content", "")
    max_results = int(arguments.get("max_results", 50))
    if not directory:
        return make_response(req_id, make_tool_text_response("Error: directory is required", is_error=True))
    try:
        d = Path(directory)
        if not d.exists():
            return make_response(req_id, make_tool_text_response(f"Error: directory not found: {directory}", is_error=True))
        matches = []
        for root, dirs, files in os.walk(str(d)):
            dirs[:] = [dd for dd in dirs if not dd.startswith(".")]
            for fname in files:
                if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                    fpath = Path(root) / fname
                    if search_content:
                        try:
                            text = fpath.read_text(encoding="utf-8", errors="replace")
                            if search_content.lower() in text.lower():
                                matches.append(str(fpath))
                        except (PermissionError, OSError):
                            pass
                    else:
                        matches.append(str(fpath))
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        if not matches:
            note = f" containing '{search_content}'" if search_content else ""
            return make_response(req_id, make_tool_text_response(
                f"No files matching '{pattern}'{note} found in {directory}"
            ))
        result = f"Found {len(matches)} file(s) (limit {max_results}):\n" + "\n".join(matches)
        return make_response(req_id, make_tool_text_response(result))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_fs_disk_info(req_id, arguments: dict) -> dict:
    try:
        lines = ["Disk Usage", "─" * 55]
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    usage = shutil.disk_usage(drive)
                    total_gb = usage.total / (1024 ** 3)
                    used_gb = usage.used / (1024 ** 3)
                    free_gb = usage.free / (1024 ** 3)
                    pct = usage.used / usage.total * 100 if usage.total > 0 else 0
                    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                    lines.append(
                        f"{drive}  [{bar}] {pct:4.0f}%  "
                        f"{used_gb:.1f}/{total_gb:.1f} GB  free: {free_gb:.1f} GB"
                    )
                except (PermissionError, OSError):
                    lines.append(f"{drive}  (inaccessible)")
        return make_response(req_id, make_tool_text_response("\n".join(lines)))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── System Handlers ─────────────────────────────────────────────────────────

_BLOCKED_PATTERNS = [
    "format ", "format.com", "diskpart", "del /f /s /q c:\\",
    "rmdir /s /q c:\\", "rd /s /q c:\\", "rm -rf /", "dd if=",
    "reg delete hklm", "bcdedit", "shutdown /r /o",
]


def handle_sys_run(req_id, arguments: dict) -> dict:
    command = arguments.get("command", "").strip()
    working_dir = arguments.get("working_dir", str(Path.home())).strip()
    timeout = min(int(arguments.get("timeout", 30)), 120)
    if not command:
        return make_response(req_id, make_tool_text_response("Error: command is required", is_error=True))
    cmd_lower = command.lower()
    for blocked in _BLOCKED_PATTERNS:
        if blocked in cmd_lower:
            return make_response(req_id, make_tool_text_response(
                f"Error: blocked command pattern: '{blocked}'", is_error=True
            ))
    try:
        cwd = working_dir if os.path.exists(working_dir) else str(Path.home())
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout, cwd=cwd, shell=False,
        )
        parts = [f"Exit code: {result.returncode}"]
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        return make_response(req_id, make_tool_text_response(
            "\n\n".join(parts), is_error=(result.returncode != 0)
        ))
    except subprocess.TimeoutExpired:
        return make_response(req_id, make_tool_text_response(
            f"Error: timed out after {timeout}s", is_error=True
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_sys_info(req_id, arguments: dict) -> dict:
    try:
        lines = [f"OS: {platform.platform()}", f"Python: {sys.version.split()[0]}"]
        cpu_r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Processor | Select-Object -First 1 | "
             "ForEach-Object { \"CPU: $($_.Name) | Cores: $($_.NumberOfCores) | Logical: $($_.NumberOfLogicalProcessors)\" }"],
            capture_output=True, text=True, timeout=10, shell=False,
        )
        if cpu_r.stdout.strip():
            lines.append(cpu_r.stdout.strip())
        ram_r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$os = Get-CimInstance Win32_OperatingSystem; "
             "$total = [math]::Round($os.TotalVisibleMemorySize/1MB,1); "
             "$free = [math]::Round($os.FreePhysicalMemory/1MB,1); "
             "$used = [math]::Round($total - $free,1); "
             "\"RAM: ${used}GB used / ${total}GB total (${free}GB free)\""],
            capture_output=True, text=True, timeout=10, shell=False,
        )
        if ram_r.stdout.strip():
            lines.append(ram_r.stdout.strip())
        return make_response(req_id, make_tool_text_response("\n".join(lines)))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_sys_processes(req_id, arguments: dict) -> dict:
    limit = int(arguments.get("limit", 20))
    sort_by = arguments.get("sort_by", "memory")
    sort_prop = {"memory": "WorkingSet", "cpu": "CPU", "name": "Name"}.get(sort_by, "WorkingSet")
    sort_dir = "Ascending" if sort_by == "name" else "Descending"
    try:
        ps_cmd = (
            f"Get-Process | Sort-Object {sort_prop} -{sort_dir} | Select-Object -First {limit} | "
            "Format-Table Name, Id, @{N='Mem(MB)';E={[math]::Round($_.WorkingSet/1MB,1)}}, CPU -AutoSize | "
            "Out-String -Width 100"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=20, shell=False,
        )
        return make_response(req_id, make_tool_text_response(
            f"Top {limit} processes (sorted by {sort_by}):\n{result.stdout.strip()}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── Git Handlers ─────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: str, timeout: int = 15) -> tuple[str, bool]:
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout, cwd=cwd, shell=False,
        )
        out = (result.stdout + result.stderr).strip()
        return out, result.returncode != 0
    except subprocess.TimeoutExpired:
        return f"git timed out after {timeout}s", True
    except Exception as e:
        return str(e), True


def handle_git_status(req_id, arguments: dict) -> dict:
    repo = arguments.get("repo_path", "").strip() or CODEX_DEFAULT_WORKDIR
    out, err = _git(["status"], repo)
    return make_response(req_id, make_tool_text_response(out, is_error=err))


def handle_git_log(req_id, arguments: dict) -> dict:
    repo = arguments.get("repo_path", "").strip() or CODEX_DEFAULT_WORKDIR
    limit = int(arguments.get("limit", 10))
    out, err = _git(["log", f"--max-count={limit}", "--oneline", "--decorate"], repo)
    return make_response(req_id, make_tool_text_response(out, is_error=err))


def handle_git_diff(req_id, arguments: dict) -> dict:
    repo = arguments.get("repo_path", "").strip() or CODEX_DEFAULT_WORKDIR
    staged = arguments.get("staged", False)
    args = ["diff", "--stat"] + (["--cached"] if staged else [])
    out, err = _git(args, repo)
    if not out.strip():
        out = "(no diff — working tree is clean)"
    return make_response(req_id, make_tool_text_response(out, is_error=err))


def handle_git_commit(req_id, arguments: dict) -> dict:
    repo = arguments.get("repo_path", "").strip() or CODEX_DEFAULT_WORKDIR
    message = arguments.get("message", "").strip()
    files = arguments.get("files", [])
    if not message:
        return make_response(req_id, make_tool_text_response("Error: message is required", is_error=True))
    add_out, add_err = _git(["add"] + files, repo) if files else _git(["add", "-A"], repo)
    if add_err and "nothing to commit" not in add_out:
        return make_response(req_id, make_tool_text_response(f"Stage failed:\n{add_out}", is_error=True))
    commit_out, commit_err = _git(["commit", "-m", message], repo)
    return make_response(req_id, make_tool_text_response(
        f"Stage:\n{add_out}\n\nCommit:\n{commit_out}", is_error=commit_err
    ))


# ─── Obsidian Vault Handlers ─────────────────────────────────────────────────

def _vault_path(rel: str) -> Path:
    """Resolve relative vault path, block path traversal."""
    p = (VAULT_ROOT / rel).resolve()
    if not str(p).startswith(str(VAULT_ROOT.resolve())):
        raise ValueError(f"Path outside vault: {rel}")
    return p


def handle_vault_read(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = _vault_path(path)
        if not p.exists():
            return make_response(req_id, make_tool_text_response(f"Not found: {path}", is_error=True))
        content = p.read_text(encoding="utf-8", errors="replace")
        return make_response(req_id, make_tool_text_response(f"# {path}\n---\n{content}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_write(req_id, arguments: dict) -> dict:
    path    = arguments.get("path", "").strip()
    content = arguments.get("content", "")
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = _vault_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return make_response(req_id, make_tool_text_response(f"Written: {path} ({p.stat().st_size:,} bytes)"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_append(req_id, arguments: dict) -> dict:
    path    = arguments.get("path", "").strip()
    content = arguments.get("content", "")
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = _vault_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n" + content)
        return make_response(req_id, make_tool_text_response(f"Appended to: {path}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_list(req_id, arguments: dict) -> dict:
    rel = arguments.get("path", "").strip() or "."
    try:
        p = _vault_path(rel)
        if not p.is_dir():
            return make_response(req_id, make_tool_text_response(f"Not a directory: {rel}", is_error=True))
        entries = []
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name.startswith("."):
                continue
            kind = "📁" if item.is_dir() else "📄"
            entries.append(f"{kind} {item.name}")
        body = "\n".join(entries) if entries else "(empty)"
        return make_response(req_id, make_tool_text_response(f"Vault/{rel}\n{'─'*40}\n{body}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_search(req_id, arguments: dict) -> dict:
    query       = arguments.get("query", "").strip()
    max_results = int(arguments.get("max_results", 20))
    if not query:
        return make_response(req_id, make_tool_text_response("Error: query is required", is_error=True))
    try:
        results = []
        query_lower = query.lower()
        for md_file in VAULT_ROOT.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.parts):
                continue
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
                if query_lower in text.lower():
                    rel = md_file.relative_to(VAULT_ROOT)
                    # Find snippet
                    idx = text.lower().find(query_lower)
                    start = max(0, idx - 60)
                    snippet = text[start:idx + 100].replace("\n", " ").strip()
                    results.append(f"📄 {rel}\n   …{snippet}…")
                    if len(results) >= max_results:
                        break
            except (PermissionError, OSError):
                pass
        if not results:
            return make_response(req_id, make_tool_text_response(f"No results for: {query}"))
        return make_response(req_id, make_tool_text_response(
            f"Found {len(results)} note(s) matching '{query}':\n\n" + "\n\n".join(results)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_delete(req_id, arguments: dict) -> dict:
    path = arguments.get("path", "").strip()
    if not path:
        return make_response(req_id, make_tool_text_response("Error: path is required", is_error=True))
    try:
        p = _vault_path(path)
        if not p.exists():
            return make_response(req_id, make_tool_text_response(f"Not found: {path}", is_error=True))
        trash = VAULT_ROOT / ".trash"
        trash.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = trash / f"{ts}_{p.name}"
        shutil.move(str(p), str(dest))
        return make_response(req_id, make_tool_text_response(f"Moved to vault .trash: {path}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_move(req_id, arguments: dict) -> dict:
    src = arguments.get("src", "").strip()
    dst = arguments.get("dst", "").strip()
    if not src or not dst:
        return make_response(req_id, make_tool_text_response("Error: src and dst are required", is_error=True))
    try:
        src_p = _vault_path(src)
        dst_p = _vault_path(dst)
        if not src_p.exists():
            return make_response(req_id, make_tool_text_response(f"Not found: {src}", is_error=True))
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return make_response(req_id, make_tool_text_response(f"Moved: {src} → {dst}"))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_daily_note(req_id, arguments: dict) -> dict:
    date_str = arguments.get("date", "").strip() or datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        week = dt.isocalendar()[1]
        day_name = dt.strftime("%A")
        rel_path = f"00 Inbox/Daily/{date_str}.md"
        p = _vault_path(rel_path)

        if p.exists():
            content = p.read_text(encoding="utf-8", errors="replace")
            return make_response(req_id, make_tool_text_response(
                f"Daily note already exists: {rel_path}\n---\n{content}"
            ))

        # Create from template
        content = f"""---
date: {date_str}
week: W{week:02d}
day: {day_name}
tags:
  - daily
---

# {date_str} {day_name}

## 🎯 今日主線
-

## ✅ 手動任務
- [ ]
- [ ]

## 🤖 Agent 對話摘要
-

## 💡 筆記 / 想法
-

## 📊 今日回顧
- **完成了：**
- **卡住了：**
- **明天優先：**
"""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return make_response(req_id, make_tool_text_response(
            f"Daily note created: {rel_path}\n---\n{content}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_recent(req_id, arguments: dict) -> dict:
    limit  = int(arguments.get("limit", 15))
    folder = arguments.get("folder", "").strip()
    try:
        root = _vault_path(folder) if folder else VAULT_ROOT
        files = [
            f for f in root.rglob("*.md")
            if not any(part.startswith(".") for part in f.parts)
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        lines = []
        for f in files[:limit]:
            rel  = f.relative_to(VAULT_ROOT)
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"{mtime}  {rel}")
        return make_response(req_id, make_tool_text_response(
            f"Recently modified ({len(lines)} notes):\n\n" + "\n".join(lines)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_tasks(req_id, arguments: dict) -> dict:
    folder = arguments.get("folder", "").strip()
    limit  = int(arguments.get("limit", 50))
    try:
        root = _vault_path(folder) if folder else VAULT_ROOT
        tasks = []
        for md_file in root.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.parts):
                continue
            try:
                rel = md_file.relative_to(VAULT_ROOT)
                for i, line in enumerate(md_file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if "- [ ]" in line:
                        tasks.append(f"[ ] {line.strip().replace('- [ ]', '').strip()}  ← {rel}:{i}")
                        if len(tasks) >= limit:
                            break
            except (PermissionError, OSError):
                pass
            if len(tasks) >= limit:
                break
        if not tasks:
            return make_response(req_id, make_tool_text_response("No unchecked tasks found! 🎉"))
        return make_response(req_id, make_tool_text_response(
            f"Unchecked tasks ({len(tasks)}):\n\n" + "\n".join(tasks)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_vault_tags(req_id, arguments: dict) -> dict:
    try:
        tag_counts: dict[str, int] = {}
        for md_file in VAULT_ROOT.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.parts):
                continue
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
                for tag in re.findall(r"(?<!\w)#([A-Za-z0-9_\-/一-鿿]+)", text):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except (PermissionError, OSError):
                pass
        if not tag_counts:
            return make_response(req_id, make_tool_text_response("No tags found in vault."))
        sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
        lines = [f"#{tag}  ({count})" for tag, count in sorted_tags]
        return make_response(req_id, make_tool_text_response(
            f"Vault tags ({len(lines)} unique):\n\n" + "\n".join(lines)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ── Template definitions (Templater syntax stripped, use simple vars) ────────
_VAULT_TEMPLATES: dict[str, str] = {
    "Daily Notes": """---
date: {date}
week: W{week}
day: {day}
tags:
  - daily
---

# {date} {day}

## 🎯 今日主線
- {title}

## ✅ 手動任務
- [ ]
- [ ]

## 🤖 Agent 對話摘要
-

## 💡 筆記 / 想法
-

## 📊 今日回顧
- **完成了：**
- **卡住了：**
- **明天優先：**
""",
    "Project": """---
project: {title}
linear_id: {linear_id}
status: 進行中
priority: {priority}
started: {date}
tags:
  - project
---

# {title}

## 一、目標 Goal
{goal}

## 二、背景 Context

## 三、已凍結方案 Decision
> ⚠️ 尚未凍結

## 四、執行步驟 Plan
- [ ] Step 1：
- [ ] Step 2：
- [ ] Step 3：

## 五、進度 Status
| 日期 | 完成 | 備註 |
|------|------|------|
| {date} | - | 建立 |

## 六、產出 Outputs

## 七、風險 Risks

## 八、下一步 Next

---

## Summary

## Related
-
""",
    "Meeting Notes": """---
date: {date}
attendees: {attendees}
topic: {title}
tags:
  - meeting
---

# 會議記錄：{title}

**日期：** {date}
**出席：** {attendees}

## 📋 議程
-

## 🗣️ 討論重點
-

## ✅ 決議事項
-

## 🔜 後續行動
| 事項 | 負責人 | 截止 |
|------|-------|------|
|  |  |  |

## 💡 備註

## Related
-
""",
    "Weekly Review": """---
week: W{week}
date_start: {date}
tags:
  - weekly-review
---

# 週回顧 W{week}

## ✅ 本週完成了什麼
-

## 🔴 卡住了什麼
-

## 💡 本週學到的
-

## 📊 指標回顧
| 指標 | 目標 | 實際 |
|------|------|------|
| Linear 完成 issue |  |  |
| 新寫筆記 |  |  |

## 🎯 下週優先
1.
2.
3.

## 💬 給下週的自己

""",
    "Decision Record": """---
date: {date}
decision: {title}
status: 提案中
tags:
  - decision
  - adr
---

# 決策記錄：{title}

**日期：** {date}
**狀態：** 提案中 → 進行中 → 已凍結 → 廢棄

## 背景
為什麼需要做這個決定？

## 選項
| 選項 | 優點 | 缺點 |
|------|------|------|
| A |  |  |
| B |  |  |

## 決策
選擇：**{title}**

理由：

## 後果
- 預期：
- 風險：

## Related
-
""",
    "Research Clipping": """---
title: {title}
source: {source}
captured: {date}
tags:
  - clipping
  - research
---

# {title}

> 來源：{source}
> 擷取：{date}

## 核心摘要
1.
2.
3.

## 重點筆記
-

## 我的想法
-

## 行動項目
- [ ]

## Related
-
""",
    "Learning Project": """---
topic: {title}
status: 進行中
started: {date}
tags:
  - learning
---

# {title}

## 🎯 學習目標
-

## 📚 來源資料
- 來源：{source}

## 🗺️ 學習地圖
- [ ] Checkpoint 1：
- [ ] Checkpoint 2：
- [ ] Checkpoint 3：

## 📝 筆記

### 核心概念

### 實作紀錄

### 卡點與解法

## 💡 我的結論 / 心得

## Related
-
""",
    "Service Subscription": """---
service: {title}
plan: {plan}
cost: {cost}
renewal: {renewal}
status: 進行中
tags:
  - service
  - subscription
---

# {title}

## 基本資訊
| 項目 | 內容 |
|------|------|
| 方案 | {plan} |
| 費用 | {cost} |
| 續費日 | {renewal} |
| 帳號 | |

## 目前用途
-

## 評估
- **值得繼續？**
- **可以替代的方案：**

## Related
-
""",
}


def handle_vault_create_from_template(req_id, arguments: dict) -> dict:
    template_name = arguments.get("template", "").strip()
    title         = arguments.get("title", "").strip()
    folder        = arguments.get("folder", "00 Inbox").strip()
    fields        = arguments.get("fields", {}) or {}

    if not template_name or not title:
        return make_response(req_id, make_tool_text_response("Error: template and title are required", is_error=True))

    # Fuzzy match template name
    match = next(
        (k for k in _VAULT_TEMPLATES if template_name.lower() in k.lower()),
        None
    )
    if not match:
        available = ", ".join(_VAULT_TEMPLATES.keys())
        return make_response(req_id, make_tool_text_response(
            f"Template '{template_name}' not found.\nAvailable: {available}", is_error=True
        ))

    now   = datetime.datetime.now()
    week  = now.isocalendar()[1]
    day   = now.strftime("%A")
    today = now.strftime("%Y-%m-%d")

    vars_: dict[str, str] = {
        "title": title, "date": today, "week": f"{week:02d}",
        "day": day, "goal": "", "linear_id": "", "priority": "P2",
        "attendees": "", "source": "", "plan": "", "cost": "", "renewal": "",
    }
    vars_.update({k: str(v) for k, v in fields.items()})

    try:
        content = _VAULT_TEMPLATES[match].format_map(vars_)
    except KeyError as missing:
        content = _VAULT_TEMPLATES[match]  # Fallback: use raw template

    safe_title = re.sub(r'[\\/:*?"<>|]', "-", title)
    rel_path   = f"{folder}/{safe_title}.md"
    p          = _vault_path(rel_path)

    if p.exists():
        return make_response(req_id, make_tool_text_response(
            f"Note already exists: {rel_path}\nUse vault_write to overwrite."
        ))

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return make_response(req_id, make_tool_text_response(
            f"Created from template '{match}': {rel_path}\n---\n{content}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── Vault Sort Inbox ─────────────────────────────────────────────────────────

# 分類規則：關鍵字 → 目標資料夾
_INBOX_RULES: list[tuple[list[str], str]] = [
    # 01 Projects
    (["project", "專案", "企劃", "建置", "規劃", "sprint", "milestone"], "01 Projects"),
    # 02 Areas（持續維護的領域）
    (["agent", "代理", "架構", "架構記錄", "architecture", "環境", "baseline",
      "hermes", "openclaw", "ollama", "ai工具", "報告", "記憶", "mem0",
      "heartbeat", "每日", "daily", "區", "狀態", "status"], "02 Areas"),
    # 03 Resources（參考資料、指令、指南）
    (["指令", "cli", "command", "指南", "guide", "教學", "tutorial", "sync",
      "api", "設定", "config", "連線", "network", "語言", "程式", "code",
      "tool", "工具", "resource", "clipping", "參考", "說明", "手冊"], "03 Resources"),
    # 04 Archive（舊驗證、對話紀錄、已完成）
    (["verify", "驗證", "archive", "封存", "紀錄整理", "對話", "結果",
      "2026-0", "2025-", "old", "舊"], "04 Archive"),
]

def _classify_inbox_note(filename: str, content_snippet: str) -> str:
    """根據檔名和內容前200字判斷目標 PARA 資料夾。"""
    text = (filename + " " + content_snippet).lower()
    for keywords, folder in _INBOX_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return folder
    return "02 Areas"  # 預設：有內容就放 Areas


def handle_vault_sort_inbox(req_id, arguments: dict) -> dict:
    dry_run = bool(arguments.get("dry_run", False))
    inbox   = VAULT_ROOT / "00 Inbox"
    skip_dirs = {"daily notes", "don't touch", "daily"}

    if not inbox.exists():
        return make_response(req_id, make_tool_text_response("Error: 00 Inbox not found", is_error=True))

    moves: list[tuple[Path, Path, str]] = []  # (src, dst, reason)
    skipped: list[str] = []

    for item in sorted(inbox.iterdir()):
        # 跳過子資料夾（只處理根層散落的 .md 檔）
        if item.is_dir():
            if item.name.lower() not in skip_dirs:
                skipped.append(f"[子資料夾跳過] {item.name}/")
            continue
        if item.suffix.lower() != ".md":
            skipped.append(f"[非md跳過] {item.name}")
            continue

        # 讀前200字做分類
        try:
            snippet = item.read_text(encoding="utf-8", errors="ignore")[:200]
        except Exception:
            snippet = ""

        target_folder = _classify_inbox_note(item.stem, snippet)
        dst = VAULT_ROOT / target_folder / item.name
        moves.append((item, dst, target_folder))

    if not moves:
        return make_response(req_id, make_tool_text_response(
            "✅ 00 Inbox 沒有散落的 .md 檔需要整理。" +
            (f"\n\n跳過項目：\n" + "\n".join(skipped) if skipped else "")
        ))

    lines = ["**vault_sort_inbox 結果**", f"模式：{'dry_run（只列出，不搬）' if dry_run else '實際搬移'}", ""]
    errors = []

    for src, dst, folder in moves:
        label = f"  {src.name}  →  {folder}/"
        if dry_run:
            lines.append(f"[預覽] {label}")
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                lines.append(f"✅ {label}")
            except Exception as e:
                lines.append(f"❌ {label}  ({e})")
                errors.append(str(e))

    if skipped:
        lines += ["", "**跳過（不動）：**"] + [f"  {s}" for s in skipped]

    summary = f"\n共 {len(moves)} 個{'預覽' if dry_run else '已搬移'}，{len(errors)} 個失敗。"
    lines.append(summary)

    return make_response(req_id, make_tool_text_response("\n".join(lines)))


# ─── Free Image Generation (Pollinations.AI) ─────────────────────────────────

def handle_image_generate_free(req_id, arguments: dict) -> dict:
    prompt = arguments.get("prompt", "").strip()
    if not prompt:
        return make_response(req_id, make_tool_text_response("Error: prompt is required", is_error=True))
    width  = int(arguments.get("width",  1024))
    height = int(arguments.get("height", 1024))
    model  = arguments.get("model", "flux").strip() or "flux"
    seed   = arguments.get("seed")

    try:
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&model={model}&nologo=true"
        if seed is not None:
            url += f"&seed={seed}"

        log(f"image_generate_free: fetching {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "handcraft-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            img_bytes = resp.read()

        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = SCREENSHOTS_DIR / f"pollinations_{ts}.png"
        fname.write_bytes(img_bytes)

        return make_response(req_id, make_tool_text_response(
            f"Image saved: {fname}\n"
            f"Prompt: {prompt}\n"
            f"Model: {model}  Size: {width}x{height}  ({len(img_bytes):,} bytes)\n"
            f"Source: Pollinations.AI (free, no key)"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── Playwright Handlers ─────────────────────────────────────────────────────

def _pw_launch():
    """Import playwright sync API lazily."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    return sync_playwright


def handle_browser_screenshot(req_id, arguments: dict) -> dict:
    url = arguments.get("url", "").strip()
    wait_ms = int(arguments.get("wait_ms", 2000))
    full_page = bool(arguments.get("full_page", False))
    if not url:
        return make_response(req_id, make_tool_text_response("Error: url is required", is_error=True))
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = SCREENSHOTS_DIR / f"screenshot_{ts}.png"
        sync_playwright = _pw_launch()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(fname), full_page=full_page)
            title = page.title()
            browser.close()
        return make_response(req_id, make_tool_text_response(
            f"Screenshot saved: {fname}\nPage title: {title}\nURL: {url}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_browser_get_text(req_id, arguments: dict) -> dict:
    url = arguments.get("url", "").strip()
    selector = arguments.get("selector", "body").strip() or "body"
    wait_ms = int(arguments.get("wait_ms", 1000))
    if not url:
        return make_response(req_id, make_tool_text_response("Error: url is required", is_error=True))
    try:
        sync_playwright = _pw_launch()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            try:
                text = page.locator(selector).first.inner_text(timeout=5000)
            except Exception:
                text = page.evaluate("document.body.innerText")
            title = page.title()
            browser.close()
        text = text.strip()
        if len(text) > 8000:
            text = text[:8000] + f"\n... (truncated, original length: {len(text)} chars)"
        return make_response(req_id, make_tool_text_response(
            f"URL: {url}\nTitle: {title}\nSelector: {selector}\n---\n{text}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_browser_run_script(req_id, arguments: dict) -> dict:
    url = arguments.get("url", "").strip()
    script = arguments.get("script", "").strip()
    wait_ms = int(arguments.get("wait_ms", 1000))
    if not url or not script:
        return make_response(req_id, make_tool_text_response("Error: url and script are required", is_error=True))
    try:
        sync_playwright = _pw_launch()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            result = page.evaluate(script)
            browser.close()
        result_str = json.dumps(result, ensure_ascii=False, indent=2) if result is not None else "null"
        if len(result_str) > 5000:
            result_str = result_str[:5000] + "\n... (truncated)"
        return make_response(req_id, make_tool_text_response(
            f"URL: {url}\nScript result:\n{result_str}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── Web Search Handler ───────────────────────────────────────────────────────

def handle_web_search(req_id, arguments: dict) -> dict:
    query = arguments.get("query", "").strip()
    if not query:
        return make_response(req_id, make_tool_text_response("Error: query is required", is_error=True))
    if not PERPLEXITY_API_KEY:
        return make_response(req_id, make_tool_text_response(
            "Error: PERPLEXITY_API_KEY not set. Add to Doppler: handcraft-mcp / prd", is_error=True
        ))
    try:
        payload = json.dumps({
            "model": "llama-3.1-sonar-small-128k-online",
            "messages": [{"role": "user", "content": query}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])
        result = content
        if citations:
            result += "\n\nSources:\n" + "\n".join(f"- {c}" for c in citations[:5])
        return make_response(req_id, make_tool_text_response(result))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


# ─── Linear Handlers ──────────────────────────────────────────────────────────

def _linear_graphql(query: str, variables: dict | None = None) -> dict:
    if not LINEAR_API_KEY:
        raise ValueError("LINEAR_API_KEY not set in Doppler")
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=payload,
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle_linear_issues(req_id, arguments: dict) -> dict:
    state = arguments.get("state", "").strip()
    limit = int(arguments.get("limit", 10))
    assignee_me = bool(arguments.get("assignee_me", False))
    try:
        filter_parts = []
        if state:
            filter_parts.append(f'state: {{ name: {{ eq: "{state}" }} }}')
        if assignee_me:
            filter_parts.append('assignee: { isMe: { eq: true } }')
        filter_clause = f"filter: {{ {', '.join(filter_parts)} }}" if filter_parts else ""
        gql = f"""
        query {{
            issues({filter_clause} first: {limit} orderBy: updatedAt) {{
                nodes {{
                    identifier title
                    state {{ name }}
                    priority
                    assignee {{ name }}
                    updatedAt
                }}
            }}
        }}
        """
        data = _linear_graphql(gql)
        issues = data["data"]["issues"]["nodes"]
        if not issues:
            return make_response(req_id, make_tool_text_response("No issues found."))
        prio_map = {0: "—", 1: "🔴 Urgent", 2: "🟠 High", 3: "🟡 Medium", 4: "🟢 Low"}
        lines = []
        for iss in issues:
            prio = prio_map.get(iss.get("priority", 0), "—")
            assignee = iss.get("assignee", {}) or {}
            lines.append(
                f"[{iss['identifier']}] {iss['title']}\n"
                f"  State: {iss['state']['name']}  Priority: {prio}  "
                f"Assignee: {assignee.get('name', '—')}"
            )
        return make_response(req_id, make_tool_text_response(
            f"Linear issues ({len(issues)}):\n\n" + "\n\n".join(lines)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_linear_create_issue(req_id, arguments: dict) -> dict:
    title = arguments.get("title", "").strip()
    description = arguments.get("description", "").strip()
    team_name = arguments.get("team_name", "").strip()
    priority = int(arguments.get("priority", 3))
    if not title:
        return make_response(req_id, make_tool_text_response("Error: title is required", is_error=True))
    try:
        # Get team ID
        teams_data = _linear_graphql("query { teams { nodes { id name } } }")
        teams = teams_data["data"]["teams"]["nodes"]
        if not teams:
            return make_response(req_id, make_tool_text_response("Error: no teams found", is_error=True))
        team = next((t for t in teams if team_name.lower() in t["name"].lower()), teams[0])
        team_id = team["id"]

        mutation = """
        mutation CreateIssue($teamId: String!, $title: String!, $description: String, $priority: Int) {
            issueCreate(input: { teamId: $teamId, title: $title, description: $description, priority: $priority }) {
                issue { identifier title url }
            }
        }
        """
        result = _linear_graphql(mutation, {
            "teamId": team_id, "title": title,
            "description": description or None, "priority": priority,
        })
        iss = result["data"]["issueCreate"]["issue"]
        return make_response(req_id, make_tool_text_response(
            f"Created: [{iss['identifier']}] {iss['title']}\nURL: {iss['url']}"
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


def handle_linear_update_issue(req_id, arguments: dict) -> dict:
    issue_id = arguments.get("issue_id", "").strip()
    state_name = arguments.get("state", "").strip()
    comment = arguments.get("comment", "").strip()
    if not issue_id:
        return make_response(req_id, make_tool_text_response("Error: issue_id is required", is_error=True))
    try:
        results = []
        # Resolve issue UUID from identifier
        find_q = f"""
        query {{
            issues(filter: {{ identifier: {{ eq: "{issue_id}" }} }} first: 1) {{
                nodes {{ id identifier title team {{ states {{ nodes {{ id name }} }} }} }}
            }}
        }}
        """
        data = _linear_graphql(find_q)
        nodes = data["data"]["issues"]["nodes"]
        if not nodes:
            return make_response(req_id, make_tool_text_response(f"Issue not found: {issue_id}", is_error=True))
        iss = nodes[0]
        iss_uuid = iss["id"]

        if state_name:
            states = iss["team"]["states"]["nodes"]
            state = next((s for s in states if state_name.lower() in s["name"].lower()), None)
            if not state:
                available = ", ".join(s["name"] for s in states)
                return make_response(req_id, make_tool_text_response(
                    f"State '{state_name}' not found. Available: {available}", is_error=True
                ))
            update_m = """
            mutation UpdateIssue($id: String!, $stateId: String!) {
                issueUpdate(id: $id, input: { stateId: $stateId }) {
                    issue { identifier state { name } }
                }
            }
            """
            upd = _linear_graphql(update_m, {"id": iss_uuid, "stateId": state["id"]})
            updated = upd["data"]["issueUpdate"]["issue"]
            results.append(f"State updated → {updated['state']['name']}")

        if comment:
            comment_m = """
            mutation AddComment($issueId: String!, $body: String!) {
                commentCreate(input: { issueId: $issueId, body: $body }) {
                    comment { id }
                }
            }
            """
            _linear_graphql(comment_m, {"issueId": iss_uuid, "body": comment})
            results.append("Comment added")

        if not results:
            return make_response(req_id, make_tool_text_response("Nothing to update (provide state or comment)"))

        return make_response(req_id, make_tool_text_response(
            f"[{issue_id}] {iss['title']}\n" + "\n".join(results)
        ))
    except Exception as e:
        return make_response(req_id, make_tool_text_response(f"Error: {e}", is_error=True))


if __name__ == "__main__":
    main()
