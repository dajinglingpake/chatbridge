from __future__ import annotations

import unittest

from tools.operations_server import handle_request


class ChatBridgeMcpServerTests(unittest.TestCase):
    def test_initialize_returns_tool_capability(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}},
            }
        )
        assert response is not None
        self.assertEqual("2025-11-25", response["result"]["protocolVersion"])
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_includes_session_start_without_legacy_permission_tools(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        assert response is not None
        tool_names = {item["name"] for item in response["result"]["tools"]}
        self.assertIn("start_agent_session", tool_names)
        self.assertIn("list_senders", tool_names)
        self.assertNotIn("enter_control_mode", tool_names)
        self.assertNotIn("exit_control_mode", tool_names)

    def test_tools_call_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_tool_guide", "arguments": {}},
            }
        )
        assert response is not None
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertIn("内置工具直接作用于当前发送方的当前会话", result["content"][0]["text"])

    def test_unknown_tool_returns_jsonrpc_error(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
            }
        )
        assert response is not None
        self.assertEqual(-32602, response["error"]["code"])
