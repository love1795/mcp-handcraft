"""
obsidian_server.py — local MCP server for Edgar's Obsidian vault
Vault root: D:\Edgar'sObsidianVault

Tools:
  vault_read    - read a note by relative path
  vault_write   - create or overwrite a note
  vault_append  - append text to a note
  vault_list    - list files/dirs in a vault folder
  vault_search  - full-text search across all .md files
  vault_delete  - delete a note
  vault_move    - move/rename a note (updates nothing else — no wikilink patch)
"""

import sys
import json
import os
import re
from pathlib import Path
from datetime import datetime

# ── Windows: binary stdin/stdout to avoid CRLF / BOM issues ─────────────────
if sys.platform == "win32":
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(),  os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    sys.stdin  = open(sys.stdin.fileno(),  "r", encoding="utf-8", newline="\n", closefd=False)
    sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)

VAULT_ROOT = Path(r"D:\Edgar'sObsidianVault")

# ── helpers ──────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[obsidian-mcp] {msg}", file=sys.stderr, flush=True)

def send(obj):
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

def ok(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})

def err(req_id, code, msg):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}})

def text_result(content: str):
    return {"content": [{"type": "text", "text": content}], "isError": False}

def safe_path(rel: str) -> Path:
    """Resolve a relative path inside the vault; reject path traversal."""
    rel = rel.lstrip("/\\")
    if not rel.endswith(".md"):
        rel = rel + ".md"
    resolved = (VAULT_ROOT / rel).resolve()
    if not str(resolved).startswith(str(VAULT_ROOT.resolve())):
        raise ValueError(f"Path outside vault: {rel}")
    return resolved

# ── tool implementations ─────────────────────────────────────────────────────

def tool_vault_read(args):
    path = safe_path(args["path"])
    if not path.exists():
        return text_result(f"Note not found: {args['path']}")
    content = path.read_text(encoding="utf-8")
    return text_result(content)

def tool_vault_write(args):
    path = safe_path(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(args["content"], encoding="utf-8")
    action = "Updated" if existed else "Created"
    return text_result(f"{action}: {path.relative_to(VAULT_ROOT)}")

def tool_vault_append(args):
    path = safe_path(args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n" + args["text"])
    return text_result(f"Appended to: {path.relative_to(VAULT_ROOT)}")

def tool_vault_list(args):
    folder = args.get("folder", "")
    folder = folder.lstrip("/\\")
    target = (VAULT_ROOT / folder).resolve()
    if not str(target).startswith(str(VAULT_ROOT.resolve())):
        return text_result("Invalid folder path")
    if not target.exists():
        return text_result(f"Folder not found: {folder or '(root)'}")

    lines = []
    for item in sorted(target.iterdir()):
        rel = item.relative_to(VAULT_ROOT)
        if item.is_dir():
            lines.append(f"📁 {rel}/")
        elif item.suffix == ".md":
            lines.append(f"📄 {rel}")

    if not lines:
        return text_result(f"Empty: {folder or '(root)'}")
    return text_result("\n".join(lines))

def tool_vault_search(args):
    query = args["query"].lower()
    max_results = int(args.get("max_results", 20))
    hits = []

    for md_file in VAULT_ROOT.rglob("*.md"):
        # Skip hidden dirs
        if any(part.startswith(".") for part in md_file.parts):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if query in content.lower() or query in md_file.name.lower():
            # Find matching lines for context
            matching_lines = []
            for i, line in enumerate(content.splitlines()):
                if query in line.lower():
                    matching_lines.append(f"  L{i+1}: {line.strip()[:100]}")
                if len(matching_lines) >= 3:
                    break

            rel = md_file.relative_to(VAULT_ROOT)
            hits.append(f"[[{rel.with_suffix('')}]]")
            if matching_lines:
                hits.extend(matching_lines)
            hits.append("")

        if len(hits) >= max_results * 5:
            break

    if not hits:
        return text_result(f'No results for "{query}"')
    return text_result(f'Results for "{query}":\n\n' + "\n".join(hits))

def tool_vault_delete(args):
    path = safe_path(args["path"])
    if not path.exists():
        return text_result(f"Not found: {args['path']}")
    path.unlink()
    return text_result(f"Deleted: {args['path']}")

def tool_vault_move(args):
    src = safe_path(args["from"])
    dst = safe_path(args["to"])
    if not src.exists():
        return text_result(f"Source not found: {args['from']}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return text_result(f"Moved: {args['from']} → {args['to']}")

# ── tool registry ─────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "vault_read",
        "description": "Read a note from the Obsidian vault by relative path (e.g. 'DAILY/2026-04-27' or 'Projects/openclaw'). .md is auto-appended.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to note inside vault"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "vault_write",
        "description": "Create or overwrite a note in the vault. Parent directories are created automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path (without .md)"},
                "content": {"type": "string", "description": "Full markdown content"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "vault_append",
        "description": "Append text to an existing note (or create it). Useful for adding entries without overwriting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string", "description": "Text to append (a newline is prepended automatically)"}
            },
            "required": ["path", "text"]
        }
    },
    {
        "name": "vault_list",
        "description": "List .md files and subdirectories in a vault folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Relative folder path. Omit or empty for vault root."}
            },
            "required": []
        }
    },
    {
        "name": "vault_search",
        "description": "Full-text search across all .md files in the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer", "default": 20}
            },
            "required": ["query"]
        }
    },
    {
        "name": "vault_delete",
        "description": "Delete a note from the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "vault_move",
        "description": "Move or rename a note inside the vault. Note: wikilinks in other files are NOT automatically updated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Source path"},
                "to":   {"type": "string", "description": "Destination path"}
            },
            "required": ["from", "to"]
        }
    }
]

TOOL_HANDLERS = {
    "vault_read":   tool_vault_read,
    "vault_write":  tool_vault_write,
    "vault_append": tool_vault_append,
    "vault_list":   tool_vault_list,
    "vault_search": tool_vault_search,
    "vault_delete": tool_vault_delete,
    "vault_move":   tool_vault_move,
}

# ── MCP request router ────────────────────────────────────────────────────────

def handle(msg):
    method = msg.get("method", "")
    req_id = msg.get("id")
    log(f"← {method}")

    if method == "initialize":
        ok(req_id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "obsidian-vault-local", "version": "1.0.0"}
        })
    elif method == "tools/list":
        ok(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = msg.get("params", {}).get("name", "")
        args = msg.get("params", {}).get("arguments", {})
        handler = TOOL_HANDLERS.get(name)
        if handler:
            try:
                result = handler(args)
                ok(req_id, result)
            except Exception as e:
                ok(req_id, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        else:
            err(req_id, -32601, f"Tool not found: {name}")
    elif method == "ping":
        ok(req_id, {})
    elif msg.get("jsonrpc") and "id" not in msg:
        pass  # notification, no response needed
    else:
        if req_id is not None:
            err(req_id, -32601, f"Method not found: {method}")

def main():
    log(f"Obsidian vault MCP ready — vault: {VAULT_ROOT}")
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
            handle(msg)
        except json.JSONDecodeError as e:
            log(f"JSON parse error: {e}")

if __name__ == "__main__":
    main()
