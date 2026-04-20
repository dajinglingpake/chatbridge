from __future__ import annotations

import unittest

from tools.chatbridge_mcp_server import handle_request


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

    def test_tools_list_includes_control_mode_and_session_start(self) -> None:
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
        self.assertIn("enter_control_mode", tool_names)
        self.assertIn("exit_control_mode", tool_names)
        self.assertIn("start_agent_session", tool_names)

    def test_tools_call_returns_text_content(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_manager_guide", "arguments": {}},
            }
        )
        assert response is not None
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertIn("ChatBridge 管理助手工作在独立控制平面", result["content"][0]["text"])

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
