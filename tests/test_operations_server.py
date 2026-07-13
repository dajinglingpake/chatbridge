from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.operations_server import ServerScope, _build_tool_specs, handle_request


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
        self.assertIn("restart_services", tool_names)
        self.assertIn("send_bridge_media", tool_names)
        self.assertIn("send_weixin_media", tool_names)
        self.assertNotIn("enter_control_mode", tool_names)
        self.assertNotIn("exit_control_mode", tool_names)

    def test_qq_group_scope_lists_current_group_history_tools_only(self) -> None:
        tool_names = set(_build_tool_specs(ServerScope(qq_history_scope="group", qq_group_id="811708184")))

        self.assertEqual(
            {"qq_current_group_recent_messages", "qq_current_group_search_messages"},
            tool_names,
        )
        self.assertNotIn("qq_admin_group_recent_messages", tool_names)
        self.assertNotIn("restart_services", tool_names)

    def test_qq_admin_scope_lists_admin_history_tools_only(self) -> None:
        tool_names = set(_build_tool_specs(ServerScope(qq_history_scope="admin", qq_admin_user_id="10001")))

        self.assertEqual(
            {"qq_admin_list_groups", "qq_admin_group_recent_messages", "qq_admin_group_search_messages"},
            tool_names,
        )
        self.assertNotIn("qq_current_group_recent_messages", tool_names)
        self.assertNotIn("restart_services", tool_names)

    def test_qq_group_history_tool_parses_napcat_log_and_sanitizes_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            payload = {
                "arrayMsg": {
                    "self_id": 900000001,
                    "user_id": 10001,
                    "time": 1782674955,
                    "message_type": "group",
                    "sender": {"user_id": 10001, "nickname": "nick", "card": "tester"},
                    "raw_message": "看图[CQ:image,file=secret.png,url=https://example.test/rkey-secret]",
                    "message": [
                        {"type": "text", "data": {"text": "看图"}},
                        {
                            "type": "image",
                            "data": {
                                "file": "C:/secret/source/secret.png",
                                "url": "https://example.test/rkey-secret",
                                "rkey": "do-not-return",
                                "sourcePath": "C:/secret/source/secret.png",
                            },
                        },
                    ],
                    "post_type": "message",
                    "group_id": 811708184,
                    "group_name": "测试群",
                }
            }
            (log_dir / "napcat.log").write_text(
                "2026-06-29 转化为 OB11Message " + json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            specs = _build_tool_specs(ServerScope(qq_history_scope="group", qq_group_id="811708184"))

            with patch("core.mcp_service.QQ_NAPCAT_LOG_DIR", log_dir):
                result = specs["qq_current_group_recent_messages"].handler({"limit": 10})

        self.assertTrue(result.ok)
        messages = result.data["messages"]
        self.assertEqual(1, len(messages))
        self.assertEqual("811708184", messages[0]["group_id"])
        self.assertEqual("tester", messages[0]["display_name"])
        self.assertEqual("看图", messages[0]["text"])
        self.assertEqual([{"type": "image", "name": "secret.png"}], messages[0]["media"])
        serialized = json.dumps(result.data, ensure_ascii=False)
        self.assertNotIn("rkey", serialized)
        self.assertNotIn("sourcePath", serialized)
        self.assertNotIn("https://example.test", serialized)

    def test_qq_group_history_scope_ignores_group_id_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            payload = {
                "arrayMsg": {
                    "user_id": 10001,
                    "time": 1782674956,
                    "message_type": "group",
                    "sender": {"nickname": "member"},
                    "raw_message": "当前群消息",
                    "message": [{"type": "text", "data": {"text": "当前群消息"}}],
                    "post_type": "message",
                    "group_id": 811708184,
                    "group_name": "测试群",
                }
            }
            (log_dir / "napcat.log").write_text(
                "转化为 OB11Message " + json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            specs = _build_tool_specs(ServerScope(qq_history_scope="group", qq_group_id="811708184"))

            with patch("core.mcp_service.QQ_NAPCAT_LOG_DIR", log_dir):
                result = specs["qq_current_group_recent_messages"].handler({"group_id": "999999", "limit": 10})

        self.assertTrue(result.ok)
        self.assertEqual("811708184", result.data["group_id"])
        self.assertEqual(1, len(result.data["messages"]))

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

    def test_restart_services_tool_calls_handler(self) -> None:
        from unittest.mock import patch

        with patch("tools.operations_server.restart_services") as mocked_restart:
            mocked_restart.return_value = type("Result", (), {"ok": True, "summary": "已安排重启", "data": {}})()
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "restart_services", "arguments": {"scope": "bridge"}},
                }
            )
        assert response is not None
        mocked_restart.assert_called_once_with("bridge")
        self.assertFalse(response["result"]["isError"])
        self.assertEqual("已安排重启", response["result"]["content"][0]["text"])

    def test_send_weixin_media_tool_calls_handler(self) -> None:
        from unittest.mock import patch

        with patch("tools.operations_server.send_weixin_media") as mocked_send:
            mocked_send.return_value = type("Result", (), {"ok": True, "summary": "已发送文件", "data": {}})()
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "send_weixin_media",
                        "arguments": {"target_sender_id": "sender-a", "path": "docs/diagram.png"},
                    },
                }
            )
        assert response is not None
        mocked_send.assert_called_once_with("sender-a", "docs/diagram.png")
        self.assertFalse(response["result"]["isError"])
        self.assertEqual("已发送文件", response["result"]["content"][0]["text"])

    def test_send_bridge_media_tool_calls_handler(self) -> None:
        from unittest.mock import patch

        with patch("tools.operations_server.send_bridge_media") as mocked_send:
            mocked_send.return_value = type("Result", (), {"ok": True, "summary": "已发送文件", "data": {}})()
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "send_bridge_media",
                        "arguments": {"target_sender_id": "qq:private:10001", "path": "docs/diagram.png"},
                    },
                }
            )
        assert response is not None
        mocked_send.assert_called_once_with("qq:private:10001", "docs/diagram.png")
        self.assertFalse(response["result"]["isError"])
        self.assertEqual("已发送文件", response["result"]["content"][0]["text"])

    def test_qq_private_admin_media_tool_uses_current_sender(self) -> None:
        specs = _build_tool_specs(
            ServerScope(
                current_sender_id="qq:private:10001",
                qq_admin_user_id="10001",
            )
        )

        self.assertNotIn("send_weixin_media", specs)
        tool = specs["send_bridge_media"]
        self.assertEqual({"path"}, set(tool.input_schema["properties"]))
        self.assertEqual(["path"], tool.input_schema["required"])

        with patch("tools.operations_server.send_current_qq_private_admin_media") as mocked_send:
            mocked_send.return_value = type("Result", (), {"ok": True, "summary": "已发送文件", "data": {}})()
            result = tool.handler({"path": "docs/diagram.png"})

        mocked_send.assert_called_once_with("qq:private:10001", "10001", "docs/diagram.png")
        self.assertTrue(result.ok)
