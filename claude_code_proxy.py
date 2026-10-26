#!/usr/bin/env python3
"""Supply proxy inference credentials to Desktop's own Claude Code executable."""

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
load_gateways = _config.load_gateways
select_gateway = _config.select_gateway
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
    gateway = select_gateway(load_gateways(source))
    # The app checks the exact bundled engine version; never resolve a PATH-based claude.
    os.execve(str(executable), command, engine_environment(os.environ, gateway))


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "models", "command", "exec", "install", "uninstall", "status",
                   "_install-policy", "_uninstall-policy"):
        sub = commands.add_parser(action)
        sub.add_argument("--config", "--source", dest="source", type=Path, default=DEFAULT_SOURCE)
        if action in ("check", "install"):
            sub.add_argument("--app", type=Path, default=DEFAULT_APP)
        if action == "exec":
            sub.add_argument("engine", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
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
        if args.action == "check":
            version, _ = check_app(args.app)
            check_desktop_policy()
            local_plan(LIBRARY)
            print(f"Claude Desktop {version}: Code-only launcher hook is available.")
        gateway = select_gateway(load_gateways(args.source))
        models = gateway_models(gateway)
        if args.action == "models":
            print("\n".join(models))
        else:
            print(f"Gateway reachable and authenticated: {gateway.url} ({len(models)} models)")
            print("No inference was generated. Desktop installation and a new local Code session must be checked separately.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"claude-code-proxy: {error}", file=sys.stderr)
        sys.exit(1)
