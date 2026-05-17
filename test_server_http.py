"""Unit tests and smoke checks for server_http helpers."""

import subprocess
import unittest
from unittest.mock import patch

import server_http
from server_http import (
    JOBS,
    JOBS_LOCK,
    DISCORD_WEBHOOK_EVENTS,
    DISCORD_WEBHOOK_EVENTS_LOCK,
    HandcraftServerConfig,
    MCPHTTPHandler,
    ThreadingHTTPServer,
    TOOLS,
    cleanup_expired_jobs,
    create_job,
    handle_discord_webhook_payload,
    handle_agent_job_cleanup,
    handle_agent_job_list,
    handle_claude_code_agent,
    handle_tools_call,
    handle_tools_list,
    list_jobs,
    update_job,
    validate_http_startup_config,
    validate_mcp_api_token,
)


def tool_text(response):
    return response["result"]["content"][0]["text"]


def tool_is_error(response):
    return response["result"].get("isError") is True


class ServerHttpJobApiTests(unittest.TestCase):
    def setUp(self):
        with JOBS_LOCK:
            JOBS.clear()

    def test_list_jobs_sorted_and_limited(self):
        first = create_job("gemini_agent", "task-1", "C:/tmp")
        second = create_job("codex_agent", "task-2", "C:/tmp")
        third = create_job("claude_code_agent", "task-3", "C:/tmp")

        update_job(first, created_at=100.0, updated_at=100.0, status="queued")
        update_job(second, created_at=200.0, updated_at=200.0, status="running")
        update_job(third, created_at=300.0, updated_at=300.0, status="succeeded")

        jobs = list_jobs(limit=2)
        self.assertEqual(2, len(jobs))
        self.assertEqual(third, jobs[0]["job_id"])
        self.assertEqual(second, jobs[1]["job_id"])

    def test_cleanup_expired_jobs_only_removes_expired(self):
        expired = create_job("gemini_agent", "expired", "C:/tmp")
        alive = create_job("codex_agent", "alive", "C:/tmp")

        update_job(expired, expires_at=1.0)
        update_job(alive, expires_at=9999999999.0)

        removed = cleanup_expired_jobs()
        self.assertEqual(1, removed)

        with JOBS_LOCK:
            self.assertNotIn(expired, JOBS)
            self.assertIn(alive, JOBS)

    def test_agent_job_list_handler_with_status_filter(self):
        create_job("gemini_agent", "queued", "C:/tmp")
        done = create_job("codex_agent", "done", "C:/tmp")
        update_job(done, status="succeeded", created_at=500.0, updated_at=500.0)

        response = handle_agent_job_list(req_id=1, arguments={"status": "succeeded", "limit": 10})
        text = response["result"]["content"][0]["text"]

        self.assertIn("Found 1 job(s)", text)
        self.assertIn("status=succeeded", text)

    def test_agent_job_cleanup_handler_reports_count(self):
        expired = create_job("gemini_agent", "expired", "C:/tmp")
        update_job(expired, expires_at=1.0)

        response = handle_agent_job_cleanup(req_id=1, arguments={})
        text = response["result"]["content"][0]["text"]
        self.assertIn("Expired jobs removed: 1", text)


class HttpStartupConfigTests(unittest.TestCase):
    def test_mcp_api_token_requires_present_value(self):
        for raw_token in (None, "", "   ", "\t\r\n"):
            with self.subTest(raw_token=raw_token):
                with self.assertRaisesRegex(RuntimeError, "MCP_API_TOKEN must be set"):
                    validate_mcp_api_token(raw_token)

    def test_mcp_api_token_trims_configured_value(self):
        self.assertEqual("secret-token", validate_mcp_api_token("  secret-token  "))

    def test_http_startup_config_reads_environment_into_config_object(self):
        with patch.dict(
            "os.environ",
            {
                "MCP_API_TOKEN": "  secret-token  ",
                "MCP_BASE_URL": "  https://mcp.example.test  ",
            },
        ):
            config = validate_http_startup_config()

        self.assertEqual(
            HandcraftServerConfig(
                mcp_api_token="secret-token",
                base_url="https://mcp.example.test",
            ),
            config,
        )

    def test_http_servers_keep_separate_auth_config(self):
        first_config = HandcraftServerConfig(mcp_api_token="first-token", base_url="https://first.example")
        second_config = HandcraftServerConfig(mcp_api_token="second-token", base_url="https://second.example")
        first_server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHTTPHandler, config=first_config)
        second_server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHTTPHandler, config=second_config)
        try:
            self.assertEqual("first-token", first_server.config.mcp_api_token)
            self.assertEqual("second-token", second_server.config.mcp_api_token)
            self.assertEqual("https://first.example", first_server.config.base_url)
            self.assertEqual("https://second.example", second_server.config.base_url)
        finally:
            first_server.server_close()
            second_server.server_close()


class DiscordWebhookTests(unittest.TestCase):
    def setUp(self):
        with DISCORD_WEBHOOK_EVENTS_LOCK:
            DISCORD_WEBHOOK_EVENTS.clear()

    def test_discord_ping_returns_pong(self):
        status, response = handle_discord_webhook_payload({"type": 1})

        self.assertEqual(200, status)
        self.assertEqual({"type": 1}, response)

    def test_discord_message_payload_is_stored(self):
        status, response = handle_discord_webhook_payload({
            "id": "msg-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "author": {"username": "edgar"},
            "content": "hello webhook",
        })

        self.assertEqual(200, status)
        self.assertTrue(response["ok"])
        self.assertEqual("discord", response["source"])
        self.assertEqual("msg-1", response["event_id"])

        with DISCORD_WEBHOOK_EVENTS_LOCK:
            self.assertEqual(1, len(DISCORD_WEBHOOK_EVENTS))
            self.assertEqual("hello webhook", DISCORD_WEBHOOK_EVENTS[0]["content"])

    def test_discord_payload_must_be_object(self):
        status, response = handle_discord_webhook_payload(["not", "an", "object"])

        self.assertEqual(400, status)
        self.assertFalse(response["ok"])


class ClaudeCodeAgentSmokeTests(unittest.TestCase):
    def test_claude_code_agent_schema_matches_handler_contract(self):
        response = handle_tools_list(req_id=1, params={})
        listed_tools = response["result"]["tools"]
        listed_claude = next(tool for tool in listed_tools if tool["name"] == "claude_code_agent")
        source_claude = next(tool for tool in TOOLS if tool["name"] == "claude_code_agent")

        self.assertEqual(source_claude, listed_claude)
        self.assertEqual("object", listed_claude["inputSchema"]["type"])
        self.assertEqual(["task"], listed_claude["inputSchema"]["required"])
        self.assertEqual("string", listed_claude["inputSchema"]["properties"]["task"]["type"])
        self.assertEqual("string", listed_claude["inputSchema"]["properties"]["working_dir"]["type"])
        self.assertEqual("boolean", listed_claude["inputSchema"]["properties"]["async"]["type"])

    def test_claude_code_agent_missing_task_returns_tool_error(self):
        response = handle_claude_code_agent(req_id=1, arguments={})

        self.assertTrue(tool_is_error(response))
        self.assertEqual("Error: task is required", tool_text(response))

    def test_claude_code_agent_missing_command_returns_tool_call_error(self):
        original_run_agent_command = server_http.run_agent_command
        try:
            def raise_file_not_found(*args, **kwargs):
                raise FileNotFoundError

            server_http.run_agent_command = raise_file_not_found
            response = handle_tools_call(
                req_id=1,
                params={
                    "name": "claude_code_agent",
                    "arguments": {
                        "task": "say hi",
                        "working_dir": "C:/tmp",
                    },
                },
            )
        finally:
            server_http.run_agent_command = original_run_agent_command

        self.assertTrue(tool_is_error(response))
        self.assertIn("Error: claude command not found", tool_text(response))

    def test_claude_code_agent_normal_tool_call_example(self):
        completed = subprocess.CompletedProcess(
            args=["claude", "-p", "say hi"],
            returncode=0,
            stdout="hello from claude\n",
            stderr="",
        )

        calls = []
        original_run_agent_command = server_http.run_agent_command
        try:
            def fake_run_agent_command(*args, **kwargs):
                calls.append((args, kwargs))
                return completed

            server_http.run_agent_command = fake_run_agent_command
            response = handle_tools_call(
                req_id=1,
                params={
                    "name": "claude_code_agent",
                    "arguments": {
                        "task": "say hi",
                        "working_dir": "C:/tmp",
                    },
                },
            )
        finally:
            server_http.run_agent_command = original_run_agent_command

        self.assertFalse(tool_is_error(response))
        self.assertEqual("hello from claude", tool_text(response))
        self.assertEqual(1, len(calls))
        args, kwargs = calls[0]
        command = args[0]
        self.assertEqual(["cmd.exe", "/c", server_http.CLAUDE_CMD, "-p", "say hi", "--output-format", "text"], command)
        self.assertEqual("C:/tmp", kwargs["cwd"])
        self.assertIsNone(kwargs["env_overrides"]["ANTHROPIC_AUTH_TOKEN"])
        self.assertIsNone(kwargs["env_overrides"]["ANTHROPIC_API_KEY"])


if __name__ == "__main__":
    unittest.main()
