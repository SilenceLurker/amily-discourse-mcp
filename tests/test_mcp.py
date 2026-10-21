import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import ClientSession
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from discourse_mcp.cli import key_prompt
from discourse_mcp.errors import AppError
from discourse_mcp.store import Store
from discourse_mcp.vault import DPAPIVault


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name))
        vault = DPAPIVault()
        for alias in ("audit", "personal"):
            self.store.add_account(alias, {"site": "https://forum.invalid", "username": alias,
                "categories": [60], "actions": ["read", "create", "reply", "edit"], "require_approval": True},
                vault.encrypt(alias, "synthetic_mcp_key_123456789"))
        self.parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "discourse_mcp", "--state-dir", self.temporary.name, "serve", "--accounts", "audit"])

    def test_stdio_current_client_tools_drafts_and_error_boundary(self):
        async def run():
            async with Client(self.parameters) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertEqual(names, {"discourse_accounts", "discourse_read_topic", "discourse_read_post",
                    "discourse_prepare_draft", "discourse_get_draft", "discourse_publish_draft"})
                for tool in tools.tools:
                    self.assertNotIn("api_key", json.dumps(tool.input_schema))
                accounts = await client.call_tool("discourse_accounts", {})
                text = "\n".join(block.text for block in accounts.content if hasattr(block, "text"))
                self.assertIn("audit", text)
                self.assertNotIn("personal", text)
                self.assertNotIn("synthetic_mcp_key", text)
                prepared = await client.call_tool("discourse_prepare_draft", {"account": "audit", "kind": "create",
                    "raw": "已发布修复内容、严重性和首个修复版本。", "category_id": 60, "title": "安全修复公告"})
                self.assertFalse(prepared.is_error)
                draft = prepared.structured_content
                self.assertEqual(draft["revision"], 1)
                self.assertEqual(draft["payload"]["raw"], "已发布修复内容、严重性和首个修复版本。")
                blocked = await client.call_tool("discourse_publish_draft", {"account": "audit",
                    "draft_id": draft["id"], "revision": 1, "request_id": "request_mcp_123"})
                self.assertTrue(blocked.is_error)
                self.assertIn("approval", " ".join(b.text for b in blocked.content if hasattr(b, "text")))
                forbidden = await client.call_tool("discourse_prepare_draft", {"account": "personal", "kind": "create",
                    "raw": "Text", "category_id": 60, "title": "Title"})
                self.assertTrue(forbidden.is_error)
        asyncio.run(run())

    def test_stdio_legacy_initialize_list_and_call(self):
        async def run():
            async with stdio_client(self.parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    result = await session.initialize()
                    self.assertEqual(result.server_info.name, "Amily Discourse")
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 6)
                    accounts = await session.call_tool("discourse_accounts", {})
                    self.assertFalse(accounts.is_error)
        asyncio.run(run())

    def test_hidden_input_rejects_redirected_stdin(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(AppError):
                key_prompt()

    def test_cli_lists_redacted_accounts_and_disables_one(self):
        command = [sys.executable, "-m", "discourse_mcp", "--state-dir", self.temporary.name]
        output = subprocess.run(command + ["accounts"], capture_output=True, text=True, timeout=10, check=True)
        self.assertNotIn("synthetic_mcp_key", output.stdout + output.stderr)
        self.assertEqual(len(json.loads(output.stdout)["accounts"]), 2)
        subprocess.run(command + ["account-disable", "personal"], capture_output=True, timeout=10, check=True)
        self.assertFalse(self.store.account("personal", include_disabled=True)["enabled"])


if __name__ == "__main__":
    unittest.main()
