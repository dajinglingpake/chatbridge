from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core import onebot_runtime_installer as installer


class OneBotRuntimeInstallerTests(unittest.TestCase):
    def test_select_release_asset_matches_current_windows_arch(self) -> None:
        release = {
            "assets": [
                {"name": "Lagrange.OneBot_linux-x64_net9.0_SelfContained.tar.gz", "browser_download_url": "https://example.com/linux"},
                {"name": "Lagrange.OneBot_win-x64_net9.0_SelfContained.zip", "browser_download_url": "https://example.com/win"},
            ]
        }
        with patch("core.onebot_runtime_installer.platform.system", return_value="Windows"), patch("core.onebot_runtime_installer.platform.machine", return_value="AMD64"):
            asset = installer._select_release_asset(release)
        self.assertEqual("https://example.com/win", asset["browser_download_url"])

    def test_select_napcat_shell_asset_prefers_official_shell_zip(self) -> None:
        release = {
            "assets": [
                {"name": "NapCat.Framework.zip", "browser_download_url": "https://example.com/framework"},
                {"name": "NapCat.Shell.zip", "browser_download_url": "https://example.com/shell"},
            ]
        }

        asset = installer._select_napcat_shell_asset(release)

        self.assertEqual("https://example.com/shell", asset["browser_download_url"])

    def test_write_lagrange_appsettings_configures_chatbridge_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            executable = runtime_dir / ("Lagrange.OneBot.exe" if installer.IS_WINDOWS else "Lagrange.OneBot")
            executable.write_text("", encoding="utf-8")

            installer._write_lagrange_appsettings(runtime_dir)

            payload = json.loads((runtime_dir / "appsettings.json").read_text(encoding="utf-8"))
        implementations = payload["Implementations"]
        self.assertEqual("", payload["Account"]["Password"])
        self.assertFalse(payload["QrCode"]["ConsoleCompatibilityMode"])
        self.assertIn({"Type": "Http", "Host": "127.0.0.1", "Port": 3000, "AccessToken": ""}, implementations)
        self.assertIn(
            {
                "Type": "HttpPost",
                "Host": "127.0.0.1",
                "Port": 5701,
                "Suffix": "/",
                "HeartBeatInterval": 5000,
                "HeartBeatEnable": True,
                "AccessToken": "",
            },
            implementations,
        )

    def test_write_napcat_config_configures_chatbridge_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            installer._write_napcat_config(runtime_dir)

            webui = json.loads((runtime_dir / "config" / "webui.json").read_text(encoding="utf-8"))
            onebot = json.loads((runtime_dir / "config" / "onebot11.json").read_text(encoding="utf-8"))

        self.assertEqual("127.0.0.1", webui["host"])
        self.assertEqual(6099, webui["port"])
        self.assertEqual("chatbridge-local-onebot", webui["token"])
        self.assertEqual(3000, onebot["network"]["httpServers"][0]["port"])
        self.assertEqual("http://127.0.0.1:5701/", onebot["network"]["httpClients"][0]["url"])
        self.assertTrue(onebot["network"]["httpServers"][0]["enable"])
        self.assertTrue(onebot["network"]["httpClients"][0]["enable"])

    def test_write_napcat_runtime_files_creates_chatbridge_launcher_without_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            installer._write_napcat_runtime_files(runtime_dir)

            launcher = runtime_dir / installer.NAPCAT_CHATBRIDGE_LAUNCHER
            payload = launcher.read_text(encoding="utf-8")

        self.assertIn("NapCatWinBootMain.exe", payload)
        self.assertIn("QQ.exe", payload)
        self.assertIn(installer.NAPCAT_QUICK_LOGIN_ENV, payload)
        self.assertIn('process.argv.push("-q",q)', payload)
        self.assertIn("-q", payload)
        self.assertIn("onebot11_*.json", payload)
        self.assertNotIn("pause", payload.lower())

    def test_find_installed_napcat_launcher_uses_runtime_directory_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            launcher = runtime_root / installer.NAPCAT_RUNTIME_NAME / installer.NAPCAT_CHATBRIDGE_LAUNCHER
            launcher.parent.mkdir(parents=True)
            launcher.write_text("", encoding="utf-8")

            with patch("core.onebot_runtime_installer.ONEBOT_RUNTIME_DIR", runtime_root):
                self.assertEqual(launcher, installer.find_installed_napcat_launcher())

    def test_find_latest_qr_image_ignores_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_qr = root / "qr-old.png"
            new_qr = root / "qrcode.png"
            old_qr.write_bytes(b"old")
            new_qr.write_bytes(b"new")
            old_time = new_qr.stat().st_mtime - 20
            old_qr.touch()
            import os

            os.utime(old_qr, (old_time, old_time))
            with patch("core.onebot_runtime_installer._qr_search_roots", return_value=[root]):
                self.assertEqual(new_qr, installer.find_latest_qr_image(since=old_time + 1))
                self.assertIsNone(installer.find_latest_qr_image(since=new_qr.stat().st_mtime + 1))

    def test_napcat_webui_credential_hashes_internal_token(self) -> None:
        calls: list[tuple[str, dict[str, object] | None]] = []

        def fake_post(path: str, *, body=None, headers=None, timeout=0):
            del headers, timeout
            calls.append((path, body))
            return {"data": {"Credential": "cred-1"}}

        with patch("core.onebot_runtime_installer._napcat_post", side_effect=fake_post):
            self.assertEqual("cred-1", installer._napcat_webui_credential(timeout=1.0))

        expected_hash = hashlib.sha256(("chatbridge-local-onebot" + ".napcat").encode("utf-8")).hexdigest()
        self.assertEqual(("/api/auth/login", {"hash": expected_hash}), calls[0])

    def test_fetch_napcat_login_qrcode_url_uses_refresh_endpoint(self) -> None:
        calls: list[str] = []

        def fake_post(path: str, *, body=None, headers=None, timeout=0):
            del body, headers, timeout
            calls.append(path)
            if path == "/api/auth/login":
                return {"data": {"Credential": "cred-1"}}
            if path == "/api/QQLogin/GetQQLoginQrcode":
                return {"data": {"qrcode": "https://q.qq.com/login"}}
            return {"data": None}

        with patch("core.onebot_runtime_installer._napcat_post", side_effect=fake_post), patch("core.onebot_runtime_installer.time.sleep", return_value=None):
            self.assertEqual("https://q.qq.com/login", installer.fetch_napcat_login_qrcode_url(refresh=True, timeout=1.0))

        self.assertEqual(["/api/auth/login", "/api/QQLogin/RefreshQRcode", "/api/QQLogin/GetQQLoginQrcode"], calls)

    def test_fetch_napcat_login_status_uses_check_login_status_endpoint(self) -> None:
        calls: list[str] = []

        def fake_post(path: str, *, body=None, headers=None, timeout=0):
            del body, headers, timeout
            calls.append(path)
            if path == "/api/auth/login":
                return {"data": {"Credential": "cred-1"}}
            if path == "/api/QQLogin/CheckLoginStatus":
                return {"data": {"isLogin": True, "isOffline": False}}
            return {"data": None}

        with patch("core.onebot_runtime_installer._napcat_post", side_effect=fake_post):
            self.assertEqual({"isLogin": True, "isOffline": False}, installer.fetch_napcat_login_status(timeout=1.0))

        self.assertEqual(["/api/auth/login", "/api/QQLogin/CheckLoginStatus"], calls)

    def test_fetch_napcat_login_info_uses_get_login_info_endpoint(self) -> None:
        calls: list[str] = []

        def fake_post(path: str, *, body=None, headers=None, timeout=0):
            del body, headers, timeout
            calls.append(path)
            if path == "/api/auth/login":
                return {"data": {"Credential": "cred-1"}}
            if path == "/api/QQLogin/GetQQLoginInfo":
                return {"data": {"uin": "2493227263", "nick": "test"}}
            return {"data": None}

        with patch("core.onebot_runtime_installer._napcat_post", side_effect=fake_post):
            self.assertEqual({"uin": "2493227263", "nick": "test"}, installer.fetch_napcat_login_info(timeout=1.0))

        self.assertEqual(["/api/auth/login", "/api/QQLogin/GetQQLoginInfo"], calls)

    def test_napcat_webui_base_urls_prefer_latest_logged_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "qq_onebot_runtime.out.log"
            log_path.write_text(
                "\n".join(
                    [
                        "WebUi User Panel Url: http://127.0.0.1:6099/webui?token=x",
                        "WebUi User Panel Url: http://127.0.0.1:6100/webui?token=x",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("core.onebot_runtime_installer.ONEBOT_RUNTIME_OUT_LOG", log_path):
                urls = installer._napcat_webui_base_urls()

        self.assertEqual("http://127.0.0.1:6100", urls[0])
        self.assertIn("http://127.0.0.1:6099", urls)

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "runtime.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "bad")

            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(RuntimeError):
                    installer._safe_extract_zip(archive, root / "extract")


if __name__ == "__main__":
    unittest.main()
