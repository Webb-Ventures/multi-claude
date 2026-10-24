#!/usr/bin/env python3
"""Create named, colour-coded macOS launchers with separate Claude Desktop profiles."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET


HERE = Path(__file__).resolve().parent
SWIFT_SOURCE = HERE / "claude_profile_launcher.swift"
DEFAULT_SOURCE = Path("/Applications/Claude.app")
DEFAULT_ROOT = Path.home() / "Library/Application Support/Claude-Profiles"
DEFAULT_APPS = Path("/Applications")
PROXY_RECEIPT = Path.home() / ".local/state/claude-code-desktop-proxy/install.json"
PROXY_POLICY = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
ENABLE_PROXY = "claudeCodeProcessWrapperEnabled"
COLORS = {"blue": "#367BF5", "purple": "#8754DB", "teal": "#168D87", "green": "#2D915A",
          "red": "#CA4553", "orange": "#D78525", "pink": "#BF5299", "slate": "#576B86",
          "black": "#000000"}


def read_json(path, default=None):
    if path.is_symlink():
        raise ValueError(f"Refusing a symlinked metadata file: {path}")
    if not path.exists():
        if default is not None:
            return default
        raise ValueError(f"Required file is missing: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path, value):
    if path.is_symlink():
        raise ValueError(f"Refusing a symlinked metadata file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".profiles-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_name(value):
    name = unicodedata.normalize("NFC", value.strip())
    if name.endswith(".app"):
        name = name[:-4].rstrip()
    if not name or name.startswith(".") or len(name.encode()) > 180:
        raise ValueError("Choose a non-empty name of at most 180 UTF-8 bytes, without a leading dot")
    if any(char in "/:" or unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("App names cannot contain slashes, colons, or control characters")
    return name


def color_value(value):
    color = COLORS.get(value.lower(), value)
    if not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
        raise ValueError("Use a named colour or a #RRGGBB value")
    return color.upper()


def recolor_svg(source, destination, color):
    with Path(source).open("rb") as stream:
        payload = stream.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("SVG must be at most 1 MiB")
    try:
        document = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("SVG must use UTF-8 encoding") from None
    if "\x00" in document or "<!DOCTYPE" in document.upper() or "<!ENTITY" in document.upper():
        raise ValueError("SVG must contain no DTD or entity definitions or null bytes")
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        raise ValueError("The icon is not valid SVG XML") from None
    namespace = "http://www.w3.org/2000/svg"
    if root.tag != "{" + namespace + "}svg":
        raise ValueError("Expected an SVG document in the SVG namespace")
    allowed = {"viewBox", "width", "height", "fill-rule", "clip-rule", "shape-rendering",
               "text-rendering", "image-rendering"}
    if set(root.attrib) - allowed or (root.text or "").strip():
        raise ValueError("Only a static, path-only SVG without scripts, styles or external resources is supported")
    try:
        box = [float(value) for value in re.split(r"[\s,]+", root.attrib["viewBox"].strip())]
    except (KeyError, ValueError):
        raise ValueError("SVG needs a numeric viewBox") from None
    if len(box) != 4 or not all(math.isfinite(value) for value in box) or box[2] <= 0 or box[3] <= 0:
        raise ValueError("SVG needs a finite, positive-size viewBox")
    paths = list(root)
    if not paths or len(paths) > 64:
        raise ValueError("SVG must contain between one and 64 paths")
    fills = []
    for path in paths:
        if (path.tag != "{" + namespace + "}path" or list(path)
                or set(path.attrib) - {"d", "fill", "fill-rule", "clip-rule"}
                or (path.text or "").strip() or (path.tail or "").strip()):
            raise ValueError("Only static SVG paths without transforms or external resources are supported")
        geometry = path.get("d", "")
        if not geometry or not re.fullmatch(r"[MmZzLlHhVvCcSsQqTtAaEe0-9+.,\s-]+", geometry):
            raise ValueError("Invalid SVG path geometry")
        fills.append(color_value(path.get("fill", "")))
    original = fills[0]
    for path, fill in zip(paths, fills):
        if fill == original:
            path.set("fill", color_value(color))
    root.set("width", "1024")
    root.set("height", "1024")
    ET.register_namespace("", namespace)
    Path(destination).write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def source_info(source):
    source = source.expanduser().resolve()
    info = plistlib.loads((source / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "com.anthropic.claudefordesktop":
        raise ValueError("The source must be the standard Claude Desktop application")
    executable = info.get("CFBundleExecutable", "Claude")
    icon = info.get("CFBundleIconFile", "electron.icns")
    if any(Path(name).name != name or name in ("", ".", "..") for name in (executable, icon)):
        raise ValueError("Unexpected source executable or icon filename")
    if not (source / "Contents/MacOS" / executable).is_file():
        raise ValueError("The source Claude executable is missing")
    return source, info


def ensure_external(source, root, apps):
    protected = [source, Path.home() / "Library/Application Support/Claude",
                 Path.home() / "Library/Application Support/Claude-3p", Path.home() / ".claude"]
    for destination in (root.resolve(), apps.resolve()):
        if any(destination == path.resolve() or path.resolve() in destination.parents for path in protected):
            raise ValueError("Profile output must be outside the source app and existing Claude data/config")


@contextmanager
def registry_lock(root):
    if root.is_symlink():
        raise ValueError("The profile root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / ".registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another profile operation is in progress") from None
        yield
    finally:
        os.close(descriptor)


def registry(root):
    result = read_json(root / "_profiles.json", {"schemaVersion": 1, "profiles": []})
    if result.get("schemaVersion") != 1 or not isinstance(result.get("profiles"), list):
        raise ValueError("Unrecognized profile registry")
    seen_ids, seen_names = set(), set()
    for entry in result["profiles"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid profile registry entry")
        try:
            profile_id = str(uuid.UUID(entry["id"]))
            name = valid_name(entry["name"])
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid profile registry entry") from None
        if entry["id"] != profile_id or entry["name"] != name or profile_id in seen_ids or name.casefold() in seen_names:
            raise ValueError("Duplicate or noncanonical profile registry entry")
        seen_ids.add(profile_id)
        seen_names.add(name.casefold())
    return result


def find_profile(data, selector):
    matches = [entry for entry in data["profiles"]
               if entry["id"] == selector or entry["name"].casefold() == selector.casefold()]
    if len(matches) != 1:
        raise ValueError("No unique profile matches that name or ID")
    return matches[0]


def proxy_available(receipt=PROXY_RECEIPT, policy=PROXY_POLICY):
    if not receipt.exists():
        return False
    record = read_json(receipt)
    managed = read_json(policy)
    if not record.get("command") or managed.get("processWrapper") != record["command"]:
        raise ValueError("The existing Code proxy installation does not match its managed launcher")
    return True


def seed_proxy(companion):
    library = companion / "configLibrary"
    if library.exists():
        raise ValueError("Refusing to overwrite an existing profile configuration library")
    profile_id = str(uuid.uuid4())
    write_json(library / (profile_id + ".json"), {ENABLE_PROXY: True})
    write_json(library / "_meta.json", {"appliedId": profile_id,
               "entries": [{"id": profile_id, "name": "Code proxy; normal Desktop sign-in"}]})


def run(command):
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"{Path(str(command[0])).name} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def build_launcher(root):
    if platform.system() != "Darwin":
        raise ValueError("This launcher builder requires macOS")
    compiler = run(["/usr/bin/xcrun", "--find", "swiftc"]).strip()
    sdk = run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"]).strip()
    stamp = str(Path(compiler).stat().st_mtime_ns).encode()
    target = platform.machine() + "-apple-macosx13.0"
    digest = hashlib.sha256(SWIFT_SOURCE.read_bytes() + target.encode() + sdk.encode() + stamp).hexdigest()[:20]
    cache = root / "_build" / digest
    binary = cache / "ProfileLauncher"
    if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK):
        return binary
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = cache / ("ProfileLauncher-" + uuid.uuid4().hex)
    try:
        run([compiler, SWIFT_SOURCE, "-sdk", sdk, "-target", target, "-O", "-framework", "AppKit", "-module-cache-path",
             cache / "ModuleCache", "-o", temporary])
        temporary.chmod(0o755)
        os.replace(temporary, binary)
    finally:
        if temporary.exists():
            temporary.unlink()
    return binary


def app_info(profile):
    return {"CFBundleIdentifier": "local.claude.profile." + profile["id"].replace("-", ""),
            "CFBundleName": profile["name"], "CFBundleDisplayName": profile["name"],
            "CFBundleExecutable": "ProfileLauncher", "CFBundleIconFile": "profile.icns",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": str(profile.get("launcherRevision", 1)), "LSMinimumSystemVersion": "13.0", "LSUIElement": True,
            "NSHighResolutionCapable": True}


def build_bundle(profile, launcher, source_icon, staging, icon_svg=None):
    bundle = staging / (profile["name"] + ".app")
    contents = bundle / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True)
    (contents / "MacOS").mkdir()
    shutil.copy2(launcher, contents / "MacOS/ProfileLauncher")
    (contents / "MacOS/ProfileLauncher").chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps(app_info(profile)))
    write_json(resources / "profile.json", profile)
    words = [word for word in profile["name"].split() if word.casefold() != "claude"]
    initials = "".join(word[0] for word in words[:2]) or "C"
    iconset = staging / "profile.iconset"
    if icon_svg is not None:
        recolor_svg(icon_svg, resources / "icon.svg", profile["color"])
        raster = staging / "icon.png"
        run(["/usr/bin/sips", "-s", "format", "png", resources / "icon.svg", "-o", raster])
        run([launcher, "--make-flat-icons", raster, iconset, profile["color"], initials])
    else:
        run([launcher, "--make-icons", source_icon, iconset, profile["color"], initials])
    run(["/usr/bin/iconutil", "--convert", "icns", "--output", resources / "profile.icns", iconset])
    run(["/usr/bin/codesign", "--force", "--sign", "-", bundle])
    run(["/usr/bin/codesign", "--verify", "--strict", bundle])
    return bundle


def create_profile(name, color, source, root, apps, shared=False, code_proxy="auto", icon_svg=None):
    name, color = valid_name(name), color_value(color)
    source, info = source_info(source)
    ensure_external(source, root, apps)
    with registry_lock(root):
        data = registry(root)
        if any(entry["name"].casefold() == name.casefold() for entry in data["profiles"]):
            raise ValueError("A profile with that name already exists")
        destination = apps / (name + ".app")
        if destination.exists() or destination.is_symlink():
            raise ValueError("The destination app already exists; it will not be overwritten")
        enabled = False if code_proxy == "off" else proxy_available()
        if code_proxy == "on" and not enabled:
            raise ValueError("Install claude_code_proxy.py first, or omit --code-proxy on")
        profile_id = str(uuid.uuid4())
        desktop = root / profile_id
        companion = root / (profile_id + "-3p")
        code = Path.home() / ".claude" if shared else root / (profile_id + "-code")
        profile = {"schemaVersion": 1, "id": profile_id, "name": name, "color": color,
                   "sourceApp": str(source), "dataDirectory": str(desktop),
                   "codeConfigDirectory": str(code), "codeProxyEnabled": enabled,
                   "sharedCodeConfig": shared, "appPath": str(destination.resolve()),
                   "iconStyle": "svg" if icon_svg is not None else "badge"}
        own_paths = [desktop, companion, *([] if shared else [code])]
        for path in own_paths:
            if path.exists() or path.is_symlink():
                raise ValueError("A new profile directory unexpectedly already exists")
        launcher = build_launcher(root)
        apps.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".claude-profile-build-", dir=apps))
        published = False
        created = []
        try:
            for path in own_paths:
                path.mkdir(mode=0o700)
                created.append(path)
            if enabled:
                seed_proxy(companion)
            icon = source / "Contents/Resources" / info.get("CFBundleIconFile", "electron.icns")
            bundle = build_bundle(profile, launcher, icon, staging, icon_svg)
            if destination.exists() or destination.is_symlink():
                raise ValueError("The destination appeared during the build; refusing to replace it")
            os.rename(bundle, destination)
            published = True
            write_json(root / "_profiles.json", {**data, "profiles": [*data["profiles"], profile]})
        except Exception:
            if published:
                shutil.rmtree(destination)
            for path in reversed(created):
                shutil.rmtree(path)
            raise
        finally:
            shutil.rmtree(staging)
    return profile


def checked_bundle(profile):
    bundle = Path(profile["appPath"])
    if bundle.is_symlink():
        raise ValueError("The launcher app must not be a symlink")
    manifest = read_json(bundle / "Contents/Resources/profile.json")
    if manifest != profile:
        raise ValueError("The launcher manifest no longer matches its registry; refusing to modify or launch it")
    return bundle


def update_icon(selector, root, color=None, icon_svg=None, apps=None):
    with registry_lock(root):
        data = registry(root)
        profile = find_profile(data, selector)
        original = checked_bundle(profile)
        source, info = source_info(Path(profile["sourceApp"]))
        destination = (apps / (profile["name"] + ".app")) if apps is not None else original
        destination = destination.expanduser()
        if destination.is_symlink():
            raise ValueError("The destination app must not be a symlink")
        destination = destination.resolve()
        ensure_external(source, root, destination.parent)
        if destination != original and (destination.exists() or destination.is_symlink()):
            raise ValueError("The destination already exists; refusing to replace another app")
        if icon_svg is None and profile.get("iconStyle") == "svg":
            icon_svg = original / "Contents/Resources/icon.svg"
        updated = {**profile, "color": color_value(color or profile["color"]),
                   "appPath": str(destination), "iconStyle": "svg" if icon_svg is not None else "badge",
                   "launcherRevision": int(profile.get("launcherRevision", 1)) + 1}
        backup = original.with_name(".icon-backup-" + profile["id"] + ".app")
        if backup.exists() or backup.is_symlink():
            raise ValueError("A previous icon update needs inspection before retrying")
        launcher = build_launcher(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".claude-icon-build-", dir=destination.parent))
        try:
            icon = source / "Contents/Resources" / info.get("CFBundleIconFile", "electron.icns")
            bundle = build_bundle(updated, launcher, icon, staging, icon_svg)
            if destination != original and (destination.exists() or destination.is_symlink()):
                raise ValueError("The destination appeared during the build; refusing to replace it")
            os.rename(original, backup)
            published = False
            try:
                os.rename(bundle, destination)
                published = True
                entries = [updated if entry["id"] == profile["id"] else entry for entry in data["profiles"]]
                write_json(root / "_profiles.json", {**data, "profiles": entries})
            except Exception:
                if published:
                    shutil.rmtree(destination)
                os.rename(backup, original)
                raise
            shutil.rmtree(backup)
        finally:
            shutil.rmtree(staging)
    return updated


def running_profiles(profiles):
    result = {profile["id"]: [] for profile in profiles}
    output = subprocess.check_output(["/bin/ps", "-axo", "pid=,comm="], text=True)
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[1].endswith("/Contents/MacOS/Claude"):
            continue
        pid = int(parts[0])
        command = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True).stdout
        for profile in profiles:
            flag = "--user-data-dir=" + profile["dataDirectory"]
            if re.search(re.escape(flag) + r"(?=\s|$)", command):
                result[profile["id"]].append(pid)
    return result


def remove_profile(selector, root):
    with registry_lock(root):
        data = registry(root)
        profile = find_profile(data, selector)
        bundle = checked_bundle(profile)
        if running_profiles([profile])[profile["id"]]:
            raise ValueError("This profile is running. Quit that instance first; nothing was stopped")
        parked = bundle.with_name(".removed-" + profile["id"] + ".app")
        if parked.exists() or parked.is_symlink():
            raise ValueError("A previous removal needs inspection before retrying")
        os.rename(bundle, parked)
        try:
            write_json(root / "_profiles.json", {**data, "profiles": [entry for entry in data["profiles"]
                       if entry["id"] != profile["id"]]})
        except Exception:
            os.rename(parked, bundle)
            raise
        shutil.rmtree(parked)
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "list", "launch", "status", "remove", "set-icon"):
        sub = commands.add_parser(action)
        sub.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        if action in ("create", "launch", "remove", "set-icon"):
            sub.add_argument("name")
        if action == "create":
            sub.add_argument("--color", default="blue")
            sub.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
            sub.add_argument("--apps-dir", type=Path, default=DEFAULT_APPS)
            sub.add_argument("--shared-code-config", action="store_true")
            sub.add_argument("--code-proxy", choices=("auto", "on", "off"), default="auto")
        if action in ("create", "set-icon"):
            sub.add_argument("--icon-svg", type=Path, help="Recolour the first fill in a static, path-only SVG")
        if action == "set-icon":
            sub.add_argument("--color")
            sub.add_argument("--apps-dir", type=Path, help="Also relocate the existing launcher to this directory")
    args = parser.parse_args(argv)
    args.root = args.root.expanduser().resolve()
    if args.action == "create":
        profile = create_profile(args.name, args.color, args.source, args.root,
                                 args.apps_dir.expanduser().resolve(), args.shared_code_config, args.code_proxy,
                                 args.icon_svg.expanduser().resolve() if args.icon_svg else None)
        print("Created:", profile["appPath"])
        print("Desktop account data:", profile["dataDirectory"])
        print("Code config:", profile["codeConfigDirectory"])
        print("Existing Code-only proxy:", "enabled" if profile["codeProxyEnabled"] else "not enabled")
        print("Sign in separately in this profile. The source app was not copied or modified.")
    elif args.action == "set-icon":
        profile = update_icon(args.name, args.root, args.color,
                              args.icon_svg.expanduser().resolve() if args.icon_svg else None,
                              args.apps_dir.expanduser() if args.apps_dir else None)
        print("Updated launcher:", profile["appPath"])
        print("Account data and Code config unchanged:", profile["dataDirectory"], profile["codeConfigDirectory"])
    elif args.action == "remove":
        profile = remove_profile(args.name, args.root)
        print("Removed launcher:", profile["name"])
        print("Account data and Code config were kept:", profile["dataDirectory"], profile["codeConfigDirectory"])
    else:
        data = registry(args.root)
        if args.action == "launch":
            profile = find_profile(data, args.name)
            bundle = checked_bundle(profile)
            source_info(Path(profile["sourceApp"]))
            run(["/usr/bin/open", "-n", bundle])
            print("Opened:", profile["name"])
        elif args.action == "status":
            pids = running_profiles(data["profiles"])
            for profile in data["profiles"]:
                print(json.dumps({"name": profile["name"], "pids": pids[profile["id"]],
                                 "desktop_directory": profile["dataDirectory"],
                                 "code_config": profile["codeConfigDirectory"],
                                 "code_proxy_enabled": profile["codeProxyEnabled"]}))
        else:
            for profile in data["profiles"]:
                try:
                    _, info = source_info(Path(profile["sourceApp"]))
                    version = info.get("CFBundleShortVersionString", "unknown")
                except (OSError, ValueError):
                    version = "source missing"
                config = "shared" if profile["sharedCodeConfig"] else "separate"
                print(f'{profile["name"]} | {profile["color"]} | Code config: {config} | Claude {version} | {profile["appPath"]}')


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"claude-profiles: {error}", file=sys.stderr)
        sys.exit(1)
