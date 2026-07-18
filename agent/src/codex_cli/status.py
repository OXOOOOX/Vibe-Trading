"""Installation, version, and authentication checks for the local Codex CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


MINIMUM_CODEX_CLI_VERSION = "0.144.5"
CODEX_INSTALL_COMMAND = "npm install -g @openai/codex@latest"
CODEX_LOGIN_COMMAND = "codex login"
CODEX_DEVICE_LOGIN_COMMAND = "codex login --device-auth"
CODEX_NPM_LATEST_URL = "https://registry.npmjs.org/@openai%2Fcodex/latest"
_STATUS_CACHE_SECONDS = 10.0
_LATEST_VERSION_CACHE_SECONDS = 600.0
_VERSION_RE = re.compile(r"(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)")


@dataclass(frozen=True)
class CodexCliStatus:
    installed: bool
    binary: str | None
    version: str | None
    minimum_version: str
    version_supported: bool
    auth_state: Literal["authenticated", "unauthenticated", "error", "unavailable"]
    ready: bool
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_cache_lock = threading.RLock()
_cached_at = 0.0
_cached_status: CodexCliStatus | None = None
_latest_version_cached_at = 0.0
_latest_version_cache: str | None = None


def _codex_binary_setting() -> str:
    return os.getenv("VIBE_TRADING_CODEX_BIN", "codex").strip() or "codex"


def isolated_codex_home() -> Path:
    configured = os.getenv("VIBE_TRADING_CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".vibe-trading" / "codex-home").resolve()


def isolated_codex_environment() -> dict[str, str]:
    home = isolated_codex_home()
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in list(env):
        if name.upper().startswith(("CODEX_", "OPENAI_")):
            env.pop(name, None)
    env["CODEX_HOME"] = str(home)
    return env


def codex_login_command(*, device_auth: bool = False) -> str:
    home = str(isolated_codex_home())
    suffix = " --device-auth" if device_auth else ""
    if os.name == "nt":
        escaped = home.replace("'", "''")
        return f"$env:CODEX_HOME='{escaped}'; codex login{suffix}"
    escaped = home.replace("'", "'\\''")
    return f"CODEX_HOME='{escaped}' codex login{suffix}"


def resolve_codex_binary() -> str | None:
    configured = _codex_binary_setting()
    configured_path = Path(configured).expanduser()
    if configured_path.is_file():
        return str(configured_path.resolve())
    return shutil.which(configured)


def _version_tuple(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = _VERSION_RE.search(value)
    if not match:
        return None
    numeric = match.group("version").split("-", 1)[0].split("+", 1)[0]
    try:
        major, minor, patch = numeric.split(".", 2)
        return int(major), int(minor), int(patch)
    except (TypeError, ValueError):
        return None


def _run_check(binary: str, *args: str, timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=creationflags,
        env=isolated_codex_environment(),
    )


def _fetch_latest_codex_cli_version(*, timeout: float = 3.0) -> str | None:
    request = Request(
        CODEX_NPM_LATEST_URL,
        headers={"Accept": "application/json", "User-Agent": "Vibe-Trading Codex CLI status"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, URLError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    version = str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""
    return version or None


def get_latest_codex_cli_version(*, force_refresh: bool = False) -> str | None:
    global _latest_version_cached_at, _latest_version_cache
    now = time.monotonic()
    with _cache_lock:
        if (
            not force_refresh
            and _latest_version_cached_at > 0
            and now - _latest_version_cached_at < _LATEST_VERSION_CACHE_SECONDS
        ):
            return _latest_version_cache
        latest = _fetch_latest_codex_cli_version()
        _latest_version_cache = latest
        _latest_version_cached_at = now
        return latest


def inspect_codex_cli() -> CodexCliStatus:
    binary = resolve_codex_binary()
    if not binary:
        return CodexCliStatus(
            installed=False,
            binary=None,
            version=None,
            minimum_version=MINIMUM_CODEX_CLI_VERSION,
            version_supported=False,
            auth_state="unavailable",
            ready=False,
            message="Codex CLI is not installed or is not available on PATH.",
        )

    try:
        version_result = _run_check(binary, "--version", timeout=5.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CodexCliStatus(
            installed=True,
            binary=binary,
            version=None,
            minimum_version=MINIMUM_CODEX_CLI_VERSION,
            version_supported=False,
            auth_state="error",
            ready=False,
            message=f"Unable to read Codex CLI version: {exc}",
        )

    version_text = "\n".join((version_result.stdout, version_result.stderr)).strip()
    version_match = _VERSION_RE.search(version_text)
    version = version_match.group("version") if version_match else None
    current_tuple = _version_tuple(version)
    minimum_tuple = _version_tuple(MINIMUM_CODEX_CLI_VERSION)
    version_supported = bool(
        version_result.returncode == 0
        and current_tuple is not None
        and minimum_tuple is not None
        and current_tuple >= minimum_tuple
    )
    if not version_supported:
        return CodexCliStatus(
            installed=True,
            binary=binary,
            version=version,
            minimum_version=MINIMUM_CODEX_CLI_VERSION,
            version_supported=False,
            auth_state="unavailable",
            ready=False,
            message=(
                f"Codex CLI {version or 'unknown'} is below the required "
                f"version {MINIMUM_CODEX_CLI_VERSION}."
            ),
        )

    try:
        auth_result = _run_check(binary, "login", "status")
    except subprocess.TimeoutExpired:
        return CodexCliStatus(
            installed=True,
            binary=binary,
            version=version,
            minimum_version=MINIMUM_CODEX_CLI_VERSION,
            version_supported=True,
            auth_state="error",
            ready=False,
            message="Timed out while checking Codex login status.",
        )
    except OSError as exc:
        return CodexCliStatus(
            installed=True,
            binary=binary,
            version=version,
            minimum_version=MINIMUM_CODEX_CLI_VERSION,
            version_supported=True,
            auth_state="error",
            ready=False,
            message=f"Unable to check Codex login status: {exc}",
        )

    auth_text = "\n".join((auth_result.stdout, auth_result.stderr)).strip()
    authenticated = auth_result.returncode == 0 and "logged in" in auth_text.casefold()
    return CodexCliStatus(
        installed=True,
        binary=binary,
        version=version,
        minimum_version=MINIMUM_CODEX_CLI_VERSION,
        version_supported=True,
        auth_state="authenticated" if authenticated else "unauthenticated",
        ready=authenticated,
        message=(
            "Codex CLI is installed and authenticated in the isolated Vibe-Trading profile."
            if authenticated
            else "Codex CLI is installed but the isolated Vibe-Trading profile requires sign-in."
        ),
    )


def get_codex_cli_status(*, force_refresh: bool = False) -> CodexCliStatus:
    global _cached_at, _cached_status
    now = time.monotonic()
    with _cache_lock:
        if (
            not force_refresh
            and _cached_status is not None
            and now - _cached_at < _STATUS_CACHE_SECONDS
        ):
            return _cached_status
        status = inspect_codex_cli()
        _cached_status = status
        _cached_at = now
        return status


def invalidate_codex_cli_status_cache() -> None:
    global _cached_at, _cached_status
    with _cache_lock:
        _cached_at = 0.0
        _cached_status = None


def is_container_runtime() -> bool:
    if os.getenv("VIBE_TRADING_CONTAINER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return os.name != "nt" and Path("/.dockerenv").exists()


def launch_codex_login_terminal(binary: str | None = None) -> bool:
    """Open a visible, fixed `codex login` terminal on native Windows."""

    if os.name != "nt" or is_container_runtime():
        return False
    resolved = binary or resolve_codex_binary()
    if not resolved:
        return False
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    codex_home = isolated_codex_home()
    codex_home.mkdir(parents=True, exist_ok=True)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    shell_binary = str(powershell) if powershell.is_file() else (shutil.which("powershell.exe") or "powershell.exe")
    quoted_home = str(codex_home).replace("'", "''")
    quoted_binary = str(resolved).replace("'", "''")
    login_script = f"$env:CODEX_HOME='{quoted_home}'; & '{quoted_binary}' login"
    subprocess.Popen(
        [
            shell_binary,
            "-NoLogo",
            "-NoExit",
            "-Command",
            login_script,
        ],
        close_fds=True,
        creationflags=creationflags,
    )
    invalidate_codex_cli_status_cache()
    return True
