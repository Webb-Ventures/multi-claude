#!/usr/bin/env python3
"""Route Claude Code through a gateway: Desktop's local Code sessions on macOS, the CLI on Linux.

Remote Control sessions are detected and run without the gateway base URL, which
Claude Code requires for Remote Control: through a configured forward proxy, or
directly on the signed-in account with a notice.
"""

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request


HERE = Path(__file__).resolve().parent
_config_spec = importlib.util.spec_from_file_location("multi_claude_config", HERE / "multi_claude_config.py")
_config = importlib.util.module_from_spec(_config_spec)
sys.modules[_config_spec.name] = _config
_config_spec.loader.exec_module(_config)
Gateway = _config.Gateway
load_config = _config.load_config
load_gateways = _config.load_gateways
select_gateway = _config.select_gateway
_rc_spec = importlib.util.spec_from_file_location("multi_claude_rc", HERE / "multi_claude_rc.py")
_rc = importlib.util.module_from_spec(_rc_spec)
_rc_spec.loader.exec_module(_rc)
requests_remote_control = _rc.requests_remote_control
IS_MACOS = sys.platform == "darwin"
DEFAULT_SOURCE = _config.default_config_path()
DEFAULT_APP = Path("/Applications/Claude.app")
LIBRARY = Path.home() / "Library/Application Support/Claude-3p/configLibrary"
STATE = Path.home() / ".local/state/claude-code-desktop-proxy/install.json"
POLICY = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
POLICY_STATE = POLICY.with_name("desktop-proxy-launcher-state.json")
ENABLE_KEY = "claudeCodeProcessWrapperEnabled"
ROUTING_ENV = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_AWS",
    "CLAUDE_CODE_USE_CLAUDE_PLATFORM_AWS",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_AWS_BASE_URL",
)
REMOTE_CONTROL_KEYS = {"enabled", "config_dir", "forward_proxy_url", "forward_proxy_ca"}
PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
SERVICE_PREFIX = "claude-code-remote-control"
SERVICE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"
SERVICE_LOCK = STATE.with_name("linux-service")
SERVICE_MARKER = "# Managed by multi-claude claude_code_proxy.py; remove with its uninstall command."


def engine_environment(parent, gateway):
    env = parent.copy()
    for name in ROUTING_ENV:
        env.pop(name, None)
    env["ANTHROPIC_BASE_URL"] = gateway.url
    env["ANTHROPIC_AUTH_TOKEN"] = gateway.token
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    exclusions = []
    for value in (env.get("NO_PROXY", ""), env.get("no_proxy", ""), gateway.parsed.hostname):
        for host in value.split(","):
            host = host.strip()
            if host and host not in exclusions:
                exclusions.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(exclusions)
    return env


def config_path(value, name):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be a file or directory path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path or start with ~")
    return path


def remote_control_settings(settings):
    rc = settings.get("remote_control", {})
    if not isinstance(rc, dict):
        raise ValueError("remote_control must be a JSON object")
    unknown = set(rc) - REMOTE_CONTROL_KEYS
    if unknown:
        raise ValueError("Unknown remote_control setting: " + ", ".join(sorted(unknown)))
    if not isinstance(rc.get("enabled", True), bool):
        raise ValueError("remote_control.enabled must be true or false")
    if "forward_proxy_ca" in rc and "forward_proxy_url" not in rc:
        raise ValueError("remote_control.forward_proxy_ca needs remote_control.forward_proxy_url")
    if not rc.get("enabled", True):
        raise ValueError("Remote Control is turned off in this configuration (remote_control.enabled is false)")
    return rc


def remote_control_environment(parent, settings, use_config_dir=True):
    """Return (env, forward proxy URL or None) for a Remote Control session.

    Claude Code allows Remote Control only with its first-party base URL and a
    signed-in subscription, so the gateway base URL and gateway token are removed.
    A configured forward proxy must be reachable; it is never skipped silently.
    """
    rc = remote_control_settings(settings)
    env = parent.copy()
    for name in (*ROUTING_ENV, *PROXY_ENV, "NODE_EXTRA_CA_CERTS",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"):
        env.pop(name, None)
    if use_config_dir and "config_dir" in rc:
        env["CLAUDE_CONFIG_DIR"] = str(config_path(rc["config_dir"], "remote_control.config_dir"))
    if "forward_proxy_url" not in rc:
        return env, None
    try:
        forward = Gateway(rc["forward_proxy_url"], "")
    except ValueError:
        raise ValueError("Invalid remote_control.forward_proxy_url; use HTTPS, loopback HTTP, or .ts.net HTTP.") from None
    if "forward_proxy_ca" in rc:
        ca = config_path(rc["forward_proxy_ca"], "remote_control.forward_proxy_ca")
        if not ca.is_file():
            raise ValueError("remote_control.forward_proxy_ca is not a readable file")
        env["NODE_EXTRA_CA_CERTS"] = str(ca)
    try:
        with socket.create_connection((forward.parsed.hostname, forward.port), timeout=3):
            pass
    except OSError:
        raise ValueError("The Remote Control forward proxy is unreachable; not falling back to direct inference") from None
    url = forward.url.rstrip("/")
    for name in PROXY_ENV:
        env[name] = url
    exclusions = []
    gateway_hosts = [urllib.parse.urlsplit(u).hostname or "" for u in settings.get("gateway_urls", [])]
    for value in (parent.get("NO_PROXY", ""), parent.get("no_proxy", ""), "localhost,127.0.0.1,::1", *gateway_hosts):
        for host in value.split(","):
            host = host.strip()
            if host and host not in exclusions:
                exclusions.append(host)
    env["NO_PROXY"] = env["no_proxy"] = ",".join(exclusions)
    return env, url


def launch_environment(args, settings, parent, use_config_dir=True, config_dir=None):
    """Return (env, notice) for a Claude Code process started with argv ``args``.

    ``config_dir`` selects the Claude Code account directory for this one session
    (any session type) and overrides ``remote_control.config_dir``.
    """
    if requests_remote_control(args):
        env, forward = remote_control_environment(parent, settings, use_config_dir)
        if forward:
            notice = f"claude-code-proxy: Remote Control session; inference goes through the forward proxy {forward}."
        else:
            notice = ("claude-code-proxy: Remote Control session; inference goes directly to Anthropic on the "
                      "signed-in account, not through the gateway.")
    else:
        gateways = [Gateway(url, settings["gateway_token"]) for url in settings["gateway_urls"]]
        env, notice = engine_environment(parent, select_gateway(gateways)), None
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env, notice


def claude_config_dir(value):
    path = config_path(str(value), "--config-dir")
    if not path.is_dir():
        raise ValueError(f"Claude config directory {path} does not exist")
    return path


def find_claude(claude=None):
    found = str(Path(claude).expanduser()) if claude else shutil.which("claude")
    if not found:
        raise ValueError("Claude CLI was not found on PATH; pass --claude /absolute/path/to/claude")
    executable = Path(found).absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("The Claude CLI executable is unavailable")
    return executable


def run_cli(args, source, claude=None, directory=None, config_dir=None):
    """Start the Claude CLI through the gateway, or as a Remote Control session."""
    if args and args[0] == "--":
        args = args[1:]
    executable = find_claude(claude)
    if directory is not None and not directory.expanduser().is_dir():
        raise ValueError("Requested working directory does not exist")
    if config_dir is not None:
        config_dir = claude_config_dir(config_dir)
    env, notice = launch_environment(args, load_config(source), os.environ, config_dir=config_dir)
    if directory is not None:
        os.chdir(directory.expanduser())
    if notice:
        print(notice, file=sys.stderr, flush=True)
    os.execve(str(executable), [str(executable), *args], env)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError("Gateway redirected the request; no credential was sent to the redirect target")


def gateway_models(gateway):
    request = urllib.request.Request(
        gateway.url + "v1/models", headers={"Authorization": "Bearer " + gateway.token}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Gateway model check returned HTTP {error.code}; credentials are not printed") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Gateway model check failed; check its URL, authentication, and network") from None
    except (ValueError, UnicodeError):
        raise ValueError("Gateway model check returned invalid JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Gateway did not return a Models API catalogue")
    models = [item["id"] for item in payload["data"]
              if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not models:
        raise ValueError("Gateway returned no model IDs")
    return models


def wrapper_command(source):
    return json.dumps([
        str(Path(sys.executable).resolve()), "-I", str(Path(__file__).resolve()),
        "exec", "--config", str(Path(source).resolve()), "--",
    ])


def execute_engine(command, source):
    if command and command[0] == "--":
        command = command[1:]
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("Expected Desktop's absolute Claude Code executable path")
    executable = Path(command[0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Desktop's selected Claude Code executable is unavailable")
    # Desktop sets the Code session's own config directory; keep it for Remote Control too.
    env, notice = launch_environment(command[1:], load_config(source), os.environ, use_config_dir=False)
    if notice:
        print(notice, file=sys.stderr, flush=True)
    # The app checks the exact bundled engine version; never resolve a PATH-based claude.
    os.execve(str(executable), command, env)


def read_object(path, default=None):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    if not path.exists():
        if default is not None:
            return default.copy()
        raise ValueError(f"Required file is missing: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_object(path, value, mode=0o600):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".desktop-proxy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_app(app):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    version = info.get("CFBundleShortVersionString", "")
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError:
        raise ValueError("Cannot determine the Claude Desktop version") from None
    if parts < (2, 16120, 0):
        raise ValueError("This app predates the first-party Code launcher hook")
    if info.get("CFBundleIdentifier") != "com.anthropic.claudefordesktop":
        raise ValueError("Expected the standard Claude Desktop application")
    executable = app / "Contents/MacOS" / info.get("CFBundleExecutable", "Claude")
    if not executable.is_file():
        raise ValueError("Claude Desktop executable is missing")
    return version, executable


def require_quit():
    result = subprocess.run(["/usr/bin/pgrep", "-x", "Claude"], capture_output=True)
    if result.returncode == 0:
        raise ValueError("Claude is running. Finish active work and quit with Cmd+Q first; nothing was stopped.")
    if result.returncode != 1:
        raise ValueError("Cannot check whether Claude is running")


def check_desktop_policy():
    paths = (
        Path("/Library/Managed Preferences/com.anthropic.claudefordesktop.plist"),
        Path("/Library/Managed Preferences") / pwd.getpwuid(os.getuid()).pw_name / "com.anthropic.claudefordesktop.plist",
    )
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("Desktop is device-managed; have the administrator configure the Code launcher instead")


def local_plan(library):
    meta = read_object(library / "_meta.json", {"entries": []})
    if meta.get("hybridPointer"):
        raise ValueError("An organization/bootstrap deployment is selected; it will not be overridden")
    entries = meta.get("entries", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Unrecognized configuration-library metadata")
    previous = meta.get("appliedId")
    if previous is not None:
        if not isinstance(previous, str) or not re.fullmatch(r"[a-f0-9-]{36}", previous):
            raise ValueError("Invalid applied configuration ID")
        current = read_object(library / (previous + ".json"))
        allowed = {"inferenceProvider", "inferenceCredentialKind", ENABLE_KEY}
        if set(current) - allowed:
            raise ValueError("The active local profile has additional settings; refusing to replace them automatically")
    profile_id = str(uuid.uuid4())
    updated = dict(meta)
    updated["entries"] = [*entries, {"id": profile_id, "name": "Signed-in Claude + Code proxy"}]
    updated["appliedId"] = profile_id
    return previous, profile_id, updated


@contextmanager
def transaction_lock(state):
    if state.parent.is_symlink():
        raise ValueError("The installation-state directory must not be a symlink")
    state.parent.mkdir(parents=True, exist_ok=True)
    path = state.with_name(state.name + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another installation or removal is in progress") from None
        yield
    finally:
        os.close(descriptor)


def install_policy(command, uid, policy=POLICY, state=POLICY_STATE):
    if policy.parent.is_symlink():
        raise ValueError("The managed-settings directory must not be a symlink")
    settings = read_object(policy, {})
    receipt = read_object(state) if state.exists() else None
    if receipt is not None:
        if receipt.get("command") != command or receipt.get("uid") != uid:
            raise ValueError("Another desktop proxy installation is recorded; refusing to replace it")
        if settings.get("processWrapper") == command:
            return
    if not isinstance(settings.get("env", {}), dict):
        raise ValueError("Managed settings env must be an object")
    if "processWrapper" in settings or "CLAUDE_CODE_PROCESS_WRAPPER" in settings.get("env", {}):
        raise ValueError("An existing managed process wrapper will not be replaced")
    if any(settings.get(key) for key in ("forceLoginMethod", "forceLoginOrgUUID", "forceLoginGatewayUrl")):
        raise ValueError("Managed authentication policy conflicts with gateway credentials")
    mode = stat.S_IMODE(policy.stat().st_mode) if policy.exists() else 0o644
    if receipt is None:
        receipt = {"command": command, "uid": uid, "existed": policy.exists(), "mode": mode}
        write_object(state, receipt)
    try:
        write_object(policy, {**settings, "processWrapper": command}, receipt["mode"])
    except Exception:
        state.unlink()
        raise


def uninstall_policy(uid, policy=POLICY, state=POLICY_STATE):
    if policy.parent.is_symlink():
        raise ValueError("The managed-settings directory must not be a symlink")
    settings = read_object(policy, {})
    if not state.exists():
        if "processWrapper" in settings:
            raise ValueError("An unrecorded managed wrapper will not be removed")
        return
    receipt = read_object(state)
    if receipt.get("uid") != uid:
        raise ValueError("The recorded installation belongs to another macOS user")
    if "processWrapper" in settings:
        if settings["processWrapper"] != receipt.get("command"):
            raise ValueError("Managed processWrapper changed after installation; refusing to overwrite it")
        del settings["processWrapper"]
    if settings or receipt["existed"]:
        write_object(policy, settings, receipt["mode"])
    elif policy.exists():
        policy.unlink()
    state.unlink()


def privileged(action, source):
    if os.geteuid() == 0:
        raise ValueError("Run install/uninstall as your normal user; only the managed-policy step uses sudo")
    command = ["/usr/bin/sudo", str(Path(sys.executable).resolve()), "-I", str(Path(__file__).resolve()),
               action, "--config", str(source.resolve())]
    result = subprocess.run(command)
    if result.returncode:
        raise ValueError("The managed-policy step did not finish; no Desktop profile was switched")


def install(source, app, library=LIBRARY, state=STATE):
    check_app(app)
    require_quit()
    check_desktop_policy()
    if state.exists() or state.is_symlink():
        raise ValueError("A local installation is already recorded; use status or uninstall first")
    gateway = select_gateway(load_gateways(source))
    gateway_models(gateway)
    previous, profile_id, meta = local_plan(library)
    profile = library / (profile_id + ".json")
    if profile.exists() or profile.is_symlink():
        raise ValueError("The new profile path already exists")
    receipt = {"previous": previous, "profile": profile_id, "source": str(source.resolve()),
               "app": str(app.resolve()), "command": wrapper_command(source), "phase": "installing"}
    write_object(state, receipt)
    print("Installing a managed Code-only launcher; sudo is needed for the system policy file.", flush=True)
    try:
        privileged("_install-policy", source)
        write_object(profile, {ENABLE_KEY: True})
        write_object(library / "_meta.json", meta)
        write_object(state, {**receipt, "phase": "installed"})
    except Exception:
        uninstall(source, library, state)
        raise
    print("Installed. Open Claude normally and start a NEW Code → Local session.")
    print("Desktop uses normal account sign-in; only the local Code engine uses gateway credentials.")
    print("No application bundle files were modified. Chat/Cowork keep first-party routing.")


def uninstall(source, library=LIBRARY, state=STATE):
    require_quit()
    receipt = read_object(state)
    source = Path(receipt["source"])
    phase = receipt.get("phase", "installed")
    if phase not in ("installing", "installed", "removing"):
        raise ValueError("Unrecognized installation phase")
    if not isinstance(receipt["profile"], str) or not re.fullmatch(r"[a-f0-9-]{36}", receipt["profile"]):
        raise ValueError("Invalid installed configuration ID")
    profile = library / (receipt["profile"] + ".json")
    if profile.exists() or profile.is_symlink() or phase == "installed":
        if read_object(profile) != {ENABLE_KEY: True}:
            raise ValueError("The installed local profile has changed; refusing to delete it")
    meta = read_object(library / "_meta.json", {"entries": []} if phase != "installed" else None)
    entries = meta.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Unrecognized configuration-library metadata")
    updated = {**meta, "entries": [entry for entry in entries if entry.get("id") != receipt["profile"]]}
    if meta.get("appliedId") == receipt["profile"]:
        updated["appliedId"] = receipt["previous"]
    write_object(state, {**receipt, "phase": "removing"})
    privileged("_uninstall-policy", source)
    if updated != meta:
        write_object(library / "_meta.json", updated)
    if profile.exists():
        profile.unlink()
    state.unlink()
    print("Uninstalled. The previous profile selection was restored unless you selected another one meanwhile.")


def installation_status(state=STATE, library=LIBRARY, policy=POLICY):
    receipt = read_object(state)
    if receipt.get("phase", "installed") != "installed":
        raise ValueError("An interrupted installation/removal is recorded; run uninstall before installing again")
    settings = read_object(policy)
    meta = read_object(library / "_meta.json")
    if settings.get("processWrapper") != receipt["command"]:
        raise ValueError("The managed launcher does not match this installation")
    if meta.get("appliedId") != receipt["profile"]:
        raise ValueError("The Code-only profile is no longer selected")
    if read_object(library / (receipt["profile"] + ".json")) != {ENABLE_KEY: True}:
        raise ValueError("The Code-only profile has changed")
    print("Code-only launcher and first-party Desktop profile are configured.")
    print("A live new Code session has not been verified by this status check.")
    return receipt


def systemd_quote(value):
    """Quote one ExecStart/Environment word; systemd expands % specifiers and $ variables."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Service paths and arguments must not contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def service_name(name=None):
    """Unit name for one background session; each name is a separate service."""
    if name is None:
        return SERVICE_PREFIX + ".service"
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        raise ValueError("--name must be 1-40 letters, digits, '-' or '_', starting with a letter or digit")
    return f"{SERVICE_PREFIX}-{name}.service"


def service_unit(source, directory, claude, args, path_env, config_dir=None, name=None):
    command = [str(Path(sys.executable).resolve()), "-I", str(Path(__file__).resolve()), "run",
               "--config", str(Path(source).resolve()), "--claude", str(claude)]
    if config_dir is not None:
        command += ["--config-dir", str(config_dir)]
    command += ["--", *args]
    directory = str(directory)
    if any(ord(c) < 32 or ord(c) == 127 for c in directory):
        raise ValueError("The working directory must not contain control characters")
    label = f" ({name})" if name else ""
    return "\n".join([
        SERVICE_MARKER,
        "[Unit]",
        f"Description=Claude Code Remote Control through multi-claude{label}",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=" + directory.replace("%", "%%"),
        "Environment=" + systemd_quote("PATH=" + path_env),
        "ExecStart=" + " ".join(systemd_quote(part) for part in command),
        "Restart=on-failure",
        "RestartSec=15",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def systemctl(*args, check=True):
    executable = shutil.which("systemctl")
    if executable is None:
        raise ValueError("systemd is not available; run `claude_code_proxy.py run -- remote-control` "
                         "under tmux or another supervisor instead")
    env = os.environ.copy()
    if not env.get("XDG_RUNTIME_DIR"):
        # Non-interactive SSH and su sessions often lack it, and systemctl --user then cannot reach the bus.
        runtime = Path(f"/run/user/{os.getuid()}")
        if not runtime.is_dir():
            raise ValueError(f"No systemd user session for this user ({runtime} is missing); log in once, "
                             f"or run: loginctl enable-linger {pwd.getpwuid(os.getuid()).pw_name}")
        env["XDG_RUNTIME_DIR"] = str(runtime)
    result = subprocess.run([executable, "--user", *args], capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise ValueError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip() or result.returncode}")
    return result


def owned_unit(unit):
    if unit.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {unit}")
    if not unit.exists():
        return False
    if not unit.read_text().startswith(SERVICE_MARKER + "\n"):
        raise ValueError(f"{unit} was not created by this tool; it will not be replaced or removed")
    return True


def managed_units(unit_dir=SERVICE_DIR):
    units = []
    for unit in sorted(unit_dir.glob(SERVICE_PREFIX + "*.service")):
        try:
            if owned_unit(unit):
                units.append(unit)
        except (OSError, ValueError):
            continue
    return units


def unit_config_dir(unit):
    """The --config-dir a managed unit passes, or None for the default account."""
    for line in unit.read_text().splitlines():
        if line.startswith("ExecStart="):
            match = re.search(r'"--config-dir" "((?:[^"\\]|\\.)*)"', line)
            if match:
                return re.sub(r"\\(.)", r"\1", match[1]).replace("%%", "%").replace("$$", "$")
    return None


def linger_hint():
    user = pwd.getpwuid(os.getuid()).pw_name
    result = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"],
                            capture_output=True, text=True) if shutil.which("loginctl") else None
    if result is not None and result.stdout.strip() == "yes":
        return None
    prefix = "" if os.geteuid() == 0 else "sudo "
    return f"To keep it running while you are logged out: {prefix}loginctl enable-linger {user}"


def install_service(source, directory, args, claude=None, config_dir=None, name=None, unit_dir=SERVICE_DIR):
    """Install and start a systemd user service that keeps one Remote Control session running.

    Each --name is its own service, so several accounts (Claude config directories)
    or projects can each keep a Remote Control session available.
    """
    if args and args[0] == "--":
        args = args[1:]
    args = args or ["remote-control"]
    if not requests_remote_control(args):
        raise ValueError("The background service runs a Remote Control session; pass e.g. -- remote-control")
    rc = remote_control_settings(load_config(source))
    directory = (directory or Path.home()).expanduser().absolute()
    if not directory.is_dir():
        raise ValueError("Requested working directory does not exist")
    executable = find_claude(claude)
    unit_file = service_name(name)
    unit = unit_dir / unit_file
    replacing = owned_unit(unit)
    # systemd does not inherit this shell's CLAUDE_CONFIG_DIR, so record the account explicitly.
    if config_dir is None and "CLAUDE_CONFIG_DIR" in os.environ and "config_dir" not in rc:
        config_dir = os.environ["CLAUDE_CONFIG_DIR"]
    if config_dir is not None:
        config_dir = claude_config_dir(config_dir)
    account = config_dir or (config_path(rc["config_dir"], "remote_control.config_dir") if "config_dir" in rc
                             else Path.home() / ".claude")
    for other in managed_units(unit_dir):
        if other != unit and unit_config_dir(other) == (str(config_dir) if config_dir else None):
            print(f"Note: {other.name} already runs Remote Control for {account}.", file=sys.stderr)
    text = service_unit(source, directory, executable, args, os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                        config_dir, name)
    unit_dir.mkdir(parents=True, exist_ok=True)
    temporary = unit.with_name("." + unit.name + ".tmp")
    temporary.write_text(text)
    temporary.chmod(0o644)
    os.replace(temporary, unit)
    systemctl("daemon-reload")
    if replacing:
        systemctl("restart", unit_file)
    systemctl("enable", "--now", unit_file)
    print(f"{'Replaced and restarted' if replacing else 'Installed and started'} {unit_file}: "
          f"Remote Control for {account} in {directory}.")
    print("It restarts on failure and starts again at login.")
    if not (account / ".credentials.json").is_file():
        print(f"Warning: no saved sign-in found in {account}. Sign in once first: "
              f"CLAUDE_CONFIG_DIR={account} claude, then /login.", file=sys.stderr)
    hint = linger_hint()
    if hint:
        print(hint)
    print(f"Logs: journalctl --user -u {unit_file} -f")


def uninstall_service(name=None, unit_dir=SERVICE_DIR):
    unit_file = service_name(name)
    unit = unit_dir / unit_file
    removed = owned_unit(unit)
    if removed:
        systemctl("disable", "--now", unit_file, check=False)
        unit.unlink()
        systemctl("daemon-reload")
    print(f"Stopped and removed {unit_file}." if removed else f"{unit_file} is not installed; nothing to remove.")


def service_status(name=None, unit_dir=SERVICE_DIR):
    """Report one named service, or every service this tool installed."""
    if name is not None:
        unit = unit_dir / service_name(name)
        if not owned_unit(unit):
            raise ValueError(f"{unit.name} is not installed")
        units = [unit]
    else:
        units = managed_units(unit_dir)
        if not units:
            raise ValueError("No Remote Control services from this tool are installed")
    states = {}
    for unit in units:
        active = systemctl("is-active", unit.name, check=False).stdout.strip() or "unknown"
        enabled = systemctl("is-enabled", unit.name, check=False).stdout.strip() or "unknown"
        account = unit_config_dir(unit) or "default account"
        print(f"{unit.name}: {active}, {enabled} at login ({account}).")
        states[unit.name] = active
    print(f"Logs: journalctl --user -u {units[0].name} -f")
    return states


def describe_remote_control(settings):
    try:
        rc = remote_control_settings(settings)
    except ValueError as error:
        return f"Remote Control: unavailable ({error})"
    if "forward_proxy_url" not in rc:
        return "Remote Control: allowed; inference goes directly to Anthropic on the signed-in account."
    try:
        _, forward = remote_control_environment({}, settings)
    except ValueError as error:
        return f"Remote Control: refused until fixed ({error})"
    return f"Remote Control: allowed through the forward proxy {forward}."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "models", "command", "exec", "run", "install", "uninstall", "status",
                   "_install-policy", "_uninstall-policy"):
        sub = commands.add_parser(action)
        sub.add_argument("--config", "--source", dest="source", type=Path, default=DEFAULT_SOURCE)
        if action in ("check", "install"):
            sub.add_argument("--app", type=Path, default=DEFAULT_APP)
        if action in ("run", "install"):
            sub.add_argument("--claude", help="Claude CLI to start (default: claude on PATH)")
            sub.add_argument("--dir", "-C", dest="directory", type=Path, help="working directory")
            sub.add_argument("--config-dir", help="Claude config directory (account) for this session")
            sub.add_argument("args", nargs=argparse.REMAINDER, help="arguments forwarded to claude after --")
        if action in ("install", "uninstall", "status"):
            sub.add_argument("--name", help="Linux: name of one background service, for several accounts or projects")
        if action == "exec":
            sub.add_argument("engine", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not IS_MACOS and args.action in ("install", "uninstall", "status"):
        if args.action == "install":
            with transaction_lock(SERVICE_LOCK):
                install_service(args.source, args.directory, args.args, args.claude, args.config_dir, args.name)
        elif args.action == "uninstall":
            with transaction_lock(SERVICE_LOCK):
                uninstall_service(args.name)
        else:
            service_status(args.name)
        return
    if args.action in ("install", "uninstall", "status") and (
            args.name or getattr(args, "args", None) or getattr(args, "directory", None)
            or getattr(args, "claude", None) or getattr(args, "config_dir", None)):
        raise ValueError("--name, --claude, --dir, --config-dir and forwarded arguments apply to the Linux "
                         "background service only")
    if args.action == "run":
        run_cli(args.args, args.source, args.claude, args.directory, args.config_dir)
        return
    if args.action in ("_install-policy", "_uninstall-policy"):
        uid = int(os.environ.get("SUDO_UID", "-1"))
        if os.geteuid() != 0 or uid < 0:
            raise ValueError("This internal step must be invoked through sudo by the installer")
        with transaction_lock(POLICY_STATE):
            if args.action == "_install-policy":
                install_policy(wrapper_command(args.source), uid)
            else:
                uninstall_policy(uid)
    elif args.action == "install":
        with transaction_lock(STATE):
            install(args.source, args.app)
    elif args.action == "uninstall":
        with transaction_lock(STATE):
            uninstall(args.source)
    elif args.action == "status":
        installation_status()
    elif args.action == "command":
        load_gateways(args.source)
        print(wrapper_command(args.source))
    elif args.action == "exec":
        execute_engine(args.engine, args.source)
    else:
        if args.action == "check" and IS_MACOS:
            version, _ = check_app(args.app)
            check_desktop_policy()
            local_plan(LIBRARY)
            print(f"Claude Desktop {version}: Code-only launcher hook is available.")
        elif args.action == "check":
            print("Not macOS: the Desktop hook does not apply; use `run` for the CLI "
                  "or `install` for a background Remote Control service.")
        settings = load_config(args.source)
        gateway = select_gateway([Gateway(url, settings["gateway_token"]) for url in settings["gateway_urls"]])
        models = gateway_models(gateway)
        if args.action == "models":
            print("\n".join(models))
        else:
            print(f"Gateway reachable and authenticated: {gateway.url} ({len(models)} models)")
            print(describe_remote_control(settings))
            print("No inference was generated. A live session must be checked separately.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"claude-code-proxy: {error}", file=sys.stderr)
        sys.exit(1)
