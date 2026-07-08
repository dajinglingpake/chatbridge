from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.platform_compat import IS_WINDOWS
from core.runtime_paths import APP_DIR, DOWNLOAD_DIR, ONEBOT_RUNTIME_DIR, ONEBOT_RUNTIME_OUT_LOG


LAGRANGE_RELEASE_API = "https://api.github.com/repos/LagrangeDev/Lagrange.Core/releases/tags/nightly"
LAGRANGE_RUNTIME_NAME = "lagrange-onebot"
NAPCAT_RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
NAPCAT_RUNTIME_NAME = "napcat-shell"
NAPCAT_CHATBRIDGE_LAUNCHER = "chatbridge-start-napcat.cmd"
INSTALL_METADATA_FILE = "chatbridge-runtime.json"
NAPCAT_WEBUI_TOKEN = "chatbridge-local-onebot"
NAPCAT_WEBUI_BASE_URL = "http://127.0.0.1:6099"
NAPCAT_QUICK_LOGIN_ENV = "CHATBRIDGE_NAPCAT_QQ"


@dataclass(frozen=True)
class OneBotRuntimeInstallResult:
    ok: bool
    message: str
    executable_path: Path | None = None


def ensure_default_onebot_runtime() -> OneBotRuntimeInstallResult:
    napcat = find_installed_napcat_launcher()
    if napcat is not None:
        _write_napcat_runtime_files(napcat.parent)
        return OneBotRuntimeInstallResult(ok=True, message=f"QQ OneBot Runtime ready: {napcat}", executable_path=napcat)

    napcat_result = _install_napcat_shell()
    if napcat_result.ok:
        return napcat_result

    installed = _find_lagrange_executable(ONEBOT_RUNTIME_DIR / LAGRANGE_RUNTIME_NAME)
    if installed is not None:
        _write_lagrange_appsettings(installed.parent)
        return OneBotRuntimeInstallResult(ok=True, message=f"QQ OneBot Runtime ready: {installed}", executable_path=installed)

    try:
        asset = _select_release_asset(_fetch_release())
    except RuntimeError as exc:
        return OneBotRuntimeInstallResult(ok=False, message=str(exc))

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    install_dir = ONEBOT_RUNTIME_DIR / LAGRANGE_RUNTIME_NAME
    archive_path = DOWNLOAD_DIR / str(asset["name"])
    try:
        _download_asset(str(asset["browser_download_url"]), archive_path)
        _verify_digest(archive_path, str(asset.get("digest") or ""))
        _extract_archive(archive_path, install_dir)
        executable = _find_lagrange_executable(install_dir)
        if executable is None:
            return OneBotRuntimeInstallResult(ok=False, message=f"QQ OneBot Runtime install failed: executable not found under {install_dir}")
        _write_lagrange_appsettings(executable.parent)
        _write_metadata(install_dir, asset, executable)
        return OneBotRuntimeInstallResult(ok=True, message=f"QQ OneBot Runtime installed: {executable}", executable_path=executable)
    except (OSError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return OneBotRuntimeInstallResult(ok=False, message=f"QQ OneBot Runtime install failed: {exc}")


def find_default_onebot_runtime_executable() -> Path | None:
    napcat = find_installed_napcat_launcher()
    if napcat is not None:
        return napcat
    return _find_lagrange_executable(ONEBOT_RUNTIME_DIR / LAGRANGE_RUNTIME_NAME)


def find_installed_napcat_launcher() -> Path | None:
    runtime_dir = ONEBOT_RUNTIME_DIR / NAPCAT_RUNTIME_NAME
    candidates = [
        runtime_dir / NAPCAT_CHATBRIDGE_LAUNCHER,
        runtime_dir / "launcher-user.bat",
        runtime_dir / "launcher.bat",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_latest_qr_image(*, since: float | None = None) -> Path | None:
    candidates: list[Path] = []
    for root in _qr_search_roots():
        if not root.exists():
            continue
        candidates.extend(path for path in root.rglob("qr-*.png") if path.is_file())
        candidates.extend(path for path in root.rglob("qrcode.png") if path.is_file())
        candidates.extend(path for path in root.rglob("*qrcode*.png") if path.is_file())
    if since is not None:
        candidates = [path for path in candidates if path.stat().st_mtime >= since]
    if not candidates:
        return None
    return max(set(candidates), key=lambda path: path.stat().st_mtime)


def fetch_napcat_login_qrcode_url(*, refresh: bool = False, timeout: float = 3.0) -> str:
    credential = _napcat_webui_credential(timeout=timeout)
    headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
    if refresh:
        _napcat_post("/api/QQLogin/RefreshQRcode", headers=headers, timeout=timeout)
        time.sleep(0.5)
    payload = _napcat_post("/api/QQLogin/GetQQLoginQrcode", headers=headers, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("qrcode") or "").strip()


def fetch_napcat_login_status(*, timeout: float = 3.0) -> dict[str, object]:
    credential = _napcat_webui_credential(timeout=timeout)
    headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
    payload = _napcat_post("/api/QQLogin/CheckLoginStatus", headers=headers, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def fetch_napcat_login_info(*, timeout: float = 3.0) -> dict[str, object]:
    credential = _napcat_webui_credential(timeout=timeout)
    headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
    payload = _napcat_post("/api/QQLogin/GetQQLoginInfo", headers=headers, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _qr_search_roots() -> list[Path]:
    roots = [ONEBOT_RUNTIME_DIR / NAPCAT_RUNTIME_NAME, ONEBOT_RUNTIME_DIR / LAGRANGE_RUNTIME_NAME, APP_DIR / "vendor" / "napcat"]
    for env_key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        raw = str(os.environ.get(env_key) or "").strip()
        if raw:
            roots.append(Path(raw) / "NapCat")
            roots.append(Path(raw) / ".napcat")
            roots.append(Path(raw) / "napcat")
    return roots


def _napcat_webui_credential(*, timeout: float) -> str:
    token_hash = hashlib.sha256((NAPCAT_WEBUI_TOKEN + ".napcat").encode("utf-8")).hexdigest()
    payload = _napcat_post("/api/auth/login", body={"hash": token_hash}, timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("Credential") or "").strip()


def _napcat_post(path: str, *, body: dict[str, object] | None = None, headers: dict[str, str] | None = None, timeout: float) -> dict[str, object]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    last_error: Exception | None = None
    for base_url in _napcat_webui_base_urls():
        request = urllib.request.Request(
            base_url + path,
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with _open_local_url(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return {}

def _napcat_webui_base_urls() -> list[str]:
    urls: list[str] = []
    if port := _latest_napcat_webui_port():
        urls.append(f"http://127.0.0.1:{port}")
    urls.extend([NAPCAT_WEBUI_BASE_URL, "http://127.0.0.1:6100", "http://127.0.0.1:6101", "http://127.0.0.1:6102"])
    return list(dict.fromkeys(urls))

def _latest_napcat_webui_port() -> str:
    try:
        raw = ONEBOT_RUNTIME_OUT_LOG.read_bytes()[-65536:].decode("utf-8", errors="replace")
    except OSError:
        return ""
    matches = re.findall(r"WebUi User Panel Url:\s*http://127\.0\.0\.1:(\d+)/webui", raw)
    return matches[-1] if matches else ""


def _open_local_url(request: urllib.request.Request, *, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def _fetch_release() -> dict[str, Any]:
    request = urllib.request.Request(LAGRANGE_RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "chatbridge"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to query Lagrange.OneBot release: {exc}") from exc


def _fetch_napcat_release() -> dict[str, Any]:
    request = urllib.request.Request(NAPCAT_RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "chatbridge"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to query NapCat release: {exc}") from exc


def _select_napcat_shell_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("failed to query NapCat release: assets missing")
    for asset in assets:
        name = str(asset.get("name") or "")
        if name == "NapCat.Shell.zip" and asset.get("browser_download_url"):
            return asset
    raise RuntimeError("failed to find NapCat.Shell.zip asset")


def _select_release_asset(release: dict[str, Any]) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("failed to query Lagrange.OneBot release: assets missing")

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        os_part = "win"
        arch_part = "x86" if machine in {"x86", "i386", "i686"} else "x64"
        suffix = ".zip"
    elif system == "linux":
        os_part = "linux"
        arch_part = "arm64" if machine in {"aarch64", "arm64"} else "arm" if machine.startswith("arm") else "x64"
        suffix = ".tar.gz"
    elif system == "darwin":
        os_part = "osx"
        arch_part = "arm64" if machine in {"aarch64", "arm64"} else "x64"
        suffix = ".tar.gz"
    else:
        raise RuntimeError(f"unsupported platform for auto QQ OneBot runtime install: {platform.system()} {platform.machine()}")

    name_fragment = f"Lagrange.OneBot_{os_part}-{arch_part}_"
    for asset in assets:
        name = str(asset.get("name") or "")
        if name.startswith(name_fragment) and name.endswith(suffix) and asset.get("browser_download_url"):
            return asset
    raise RuntimeError(f"failed to find Lagrange.OneBot asset for {os_part}-{arch_part}")


def _install_napcat_shell() -> OneBotRuntimeInstallResult:
    if not IS_WINDOWS:
        return OneBotRuntimeInstallResult(ok=False, message="NapCat auto install is only supported on Windows")
    try:
        asset = _select_napcat_shell_asset(_fetch_napcat_release())
    except RuntimeError as exc:
        return OneBotRuntimeInstallResult(ok=False, message=str(exc))

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    install_dir = ONEBOT_RUNTIME_DIR / NAPCAT_RUNTIME_NAME
    archive_path = DOWNLOAD_DIR / str(asset["name"])
    try:
        _download_asset(str(asset["browser_download_url"]), archive_path)
        _verify_digest(archive_path, str(asset.get("digest") or ""))
        _extract_archive(archive_path, install_dir)
        _write_napcat_runtime_files(install_dir)
        launcher = find_installed_napcat_launcher()
        if launcher is None:
            return OneBotRuntimeInstallResult(ok=False, message=f"QQ OneBot Runtime install failed: NapCat launcher not found under {install_dir}")
        _write_metadata(install_dir, asset, launcher, runtime="NapCat Shell", source="NapNeko/NapCatQQ", release_api=NAPCAT_RELEASE_API)
        return OneBotRuntimeInstallResult(ok=True, message=f"QQ OneBot Runtime installed: {launcher}", executable_path=launcher)
    except (OSError, RuntimeError, urllib.error.URLError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return OneBotRuntimeInstallResult(ok=False, message=f"QQ OneBot Runtime install failed: {exc}")


def _download_asset(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        return
    temp_path = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "chatbridge"})
    with urllib.request.urlopen(request, timeout=60) as response, temp_path.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    temp_path.replace(target)


def _verify_digest(path: Path, digest: str) -> None:
    if not digest.startswith("sha256:"):
        return
    expected = digest.removeprefix("sha256:").lower()
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"digest mismatch for {path.name}: expected {expected}, got {actual}")


def _extract_archive(archive_path: Path, install_dir: Path) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, install_dir)
        return
    with tarfile.open(archive_path) as archive:
        _safe_extract_tar(archive, install_dir)


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (target_dir / member.filename).resolve()
        if target_root != member_path and target_root not in member_path.parents:
            raise RuntimeError(f"unsafe archive path: {member.filename}")
    archive.extractall(target_dir)


def _safe_extract_tar(archive: tarfile.TarFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.getmembers():
        member_path = (target_dir / member.name).resolve()
        if target_root != member_path and target_root not in member_path.parents:
            raise RuntimeError(f"unsafe archive path: {member.name}")
    archive.extractall(target_dir)


def _find_lagrange_executable(root: Path) -> Path | None:
    if not root.exists():
        return None
    names = ("Lagrange.OneBot.exe", "Lagrange.OneBot")
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for path in root.rglob("*"):
        if path.is_file() and path.name in names:
            return path
    return None


def _write_lagrange_appsettings(runtime_dir: Path) -> None:
    config_path = runtime_dir / "appsettings.json"
    payload = {
        "$schema": "https://raw.githubusercontent.com/LagrangeDev/Lagrange.Core/master/Lagrange.OneBot/Resources/appsettings_schema.json",
        "Logging": {
            "LogLevel": {
                "Default": "Information",
                "Microsoft": "Warning",
                "Microsoft.Hosting.Lifetime": "Information",
            }
        },
        "SignServerUrl": "",
        "SignProxyUrl": "",
        "MusicSignServerUrl": "",
        "Account": {
            "Uin": 0,
            "Password": "",
            "Protocol": "Windows" if IS_WINDOWS else "MacOs" if platform.system().lower() == "darwin" else "Linux",
            "AutoReconnect": True,
            "GetOptimumServer": True,
            "AutoReLogin": True,
        },
        "Message": {"IgnoreSelf": True, "StringPost": False},
        "QrCode": {"ConsoleCompatibilityMode": False},
        "Implementations": [
            {"Type": "Http", "Host": "127.0.0.1", "Port": 3000, "AccessToken": ""},
            {
                "Type": "HttpPost",
                "Host": "127.0.0.1",
                "Port": 5701,
                "Suffix": "/",
                "HeartBeatInterval": 5000,
                "HeartBeatEnable": True,
                "AccessToken": "",
            },
        ],
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    executable = _find_lagrange_executable(runtime_dir)
    if executable is not None and not IS_WINDOWS:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_napcat_runtime_files(runtime_dir: Path) -> None:
    _write_napcat_config(runtime_dir)
    _write_napcat_launcher(runtime_dir)


def _write_napcat_config(runtime_dir: Path) -> None:
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    webui = {"host": "127.0.0.1", "port": 6099, "token": NAPCAT_WEBUI_TOKEN, "loginRate": 60}
    onebot = {
        "network": {
            "httpServers": [
                {
                    "name": "chatbridge-http-api",
                    "enable": True,
                    "port": 3000,
                    "host": "127.0.0.1",
                    "enableCors": True,
                    "enableWebsocket": False,
                    "messagePostFormat": "array",
                    "token": "",
                    "debug": False,
                }
            ],
            "httpClients": [
                {
                    "name": "chatbridge-reverse-http",
                    "enable": True,
                    "url": "http://127.0.0.1:5701/",
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "token": "",
                    "debug": False,
                }
            ],
            "websocketServers": [],
            "websocketClients": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    napcat = {"fileLog": True, "consoleLog": True, "fileLogLevel": "debug", "consoleLogLevel": "info", "packetServer": ""}
    (config_dir / "webui.json").write_text(json.dumps(webui, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (config_dir / "onebot11.json").write_text(json.dumps(onebot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (config_dir / "napcat.json").write_text(json.dumps(napcat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_napcat_launcher(runtime_dir: Path) -> None:
    launcher = runtime_dir / NAPCAT_CHATBRIDGE_LAUNCHER
    payload = r"""@echo off
chcp 65001 >nul
set NAPCAT_PATCH_PACKAGE=%cd%\qqnt.json
set NAPCAT_LOAD_PATH=%cd%\loadNapCat.js
set NAPCAT_INJECT_PATH=%cd%\NapCatWinBootHook.dll
set NAPCAT_LAUNCHER_PATH=%cd%\NapCatWinBootMain.exe
set NAPCAT_MAIN_PATH=%cd%\napcat.mjs

for /f "tokens=2*" %%a in ('reg query "HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v "UninstallString"') do (
    set "RetString=%%~b"
    goto :napcat_boot
)

:napcat_boot
if not defined RetString (
    echo QQ install path was not found in registry.
    exit /b 2
)

for %%a in ("%RetString%") do (
    set "pathWithoutUninstall=%%~dpa"
)

set "QQPath=%pathWithoutUninstall%QQ.exe"
if not exist "%QQPath%" (
    echo QQ executable was not found: %QQPath%
    exit /b 2
)

set NAPCAT_MAIN_PATH=%NAPCAT_MAIN_PATH:\=/%
echo (async () =^> {await import("file:///%NAPCAT_MAIN_PATH%")})() > "%NAPCAT_LOAD_PATH%"
set "CHATBRIDGE_NAPCAT_QQ=%CHATBRIDGE_NAPCAT_QQ%"
if not defined CHATBRIDGE_NAPCAT_QQ (
    for /f "delims=" %%f in ('dir /b /o-d "%cd%\config\onebot11_*.json" 2^>nul') do (
        set "CHATBRIDGE_NAPCAT_QQ=%%~nf"
        set "CHATBRIDGE_NAPCAT_QQ=!CHATBRIDGE_NAPCAT_QQ:onebot11_=!"
        goto :napcat_launch
    )
)

:napcat_launch
if defined CHATBRIDGE_NAPCAT_QQ (
    "%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" -q "%CHATBRIDGE_NAPCAT_QQ%"
) else (
    "%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%"
)
"""
    launcher.write_text(payload.replace("@echo off", "@echo off\nsetlocal EnableDelayedExpansion", 1).replace("\n", "\r\n"), encoding="utf-8")


def _is_napcat_launcher(path: Path) -> bool:
    return path.name.lower() in {NAPCAT_CHATBRIDGE_LAUNCHER.lower(), "launcher-user.bat", "launcher.bat", "launcher-win10-user.bat", "launcher-win10.bat"}


def _write_metadata(
    install_dir: Path,
    asset: dict[str, Any],
    executable: Path,
    *,
    runtime: str = "Lagrange.OneBot",
    source: str = "LagrangeDev/Lagrange.Core",
    release_api: str = LAGRANGE_RELEASE_API,
) -> None:
    metadata = {
        "runtime": runtime,
        "source": source,
        "release_api": release_api,
        "asset": asset.get("name"),
        "download_url": asset.get("browser_download_url"),
        "digest": asset.get("digest"),
        "executable": str(executable),
    }
    (install_dir / INSTALL_METADATA_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
