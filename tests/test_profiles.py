import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "claude_profiles.py"
spec = importlib.util.spec_from_file_location("profiles_under_test", SCRIPT)
profiles = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = profiles
spec.loader.exec_module(profiles)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "profiles"
        self.apps = self.base / "Applications"
        self.source = self.base / "Claude.app"
        (self.source / "Contents/MacOS").mkdir(parents=True)
        (self.source / "Contents/MacOS/Claude").write_text("vendor executable fixture")
        (self.source / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.anthropic.claudefordesktop",
            "CFBundleExecutable": "Claude", "CFBundleIconFile": "electron.icns",
            "CFBundleShortVersionString": "3.6556.2",
        }))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(profiles, "build_launcher", return_value=self.base / "launcher").start()
        self.real_build_bundle = profiles.build_bundle
        mock.patch.object(profiles, "build_bundle", side_effect=self.fake_bundle).start()
        self.real_proxy_available = profiles.proxy_available
        self.proxy = mock.patch.object(profiles, "proxy_available", return_value=False).start()
        self.real_register_bundle = profiles.register_bundle
        self.registration = mock.patch.object(profiles, "register_bundle").start()
        self.real_check_copy_policy = profiles.check_copy_policy
        self.copy_policy = mock.patch.object(profiles, "check_copy_policy").start()

    def fake_bundle(self, profile, launcher, source_icon, staging, icon_svg=None):
        bundle = staging / (profile["name"] + ".app")
        profiles.write_json(bundle / "Contents/Resources/profile.json", profile)
        if icon_svg is not None:
            profiles.recolor_svg(icon_svg, bundle / "Contents/Resources/icon.svg", profile["color"])
        return bundle

    def svg_fixture(self):
        source = self.base / "input.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 509.64">'
                          '<path fill="#D77655" d="M0 0h512v509.64H0z"/>'
                          '<path fill="#FCF2EE" d="M20 20h30v30z"/></svg>')
        return source

    def create(self, name="Claude Work", **kwargs):
        return profiles.create_profile(name, "blue", self.source, self.root, self.apps, **kwargs)

    def test_profile_has_fresh_separate_directories_and_source_is_untouched(self):
        original = (self.source / "Contents/Info.plist").read_bytes()
        result = self.create()
        self.assertEqual(result["sourceApp"], str(self.source.resolve()))
        self.assertEqual(result["color"], "#367BF5")
        self.assertTrue(Path(result["appPath"]).is_dir())
        self.assertNotEqual(result["dataDirectory"], result["codeConfigDirectory"])
        self.assertFalse(result["sharedCodeConfig"])
        self.assertFalse(result["codeProxyEnabled"])
        self.assertEqual(Path(result["dataDirectory"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.source / "Contents/Info.plist").read_bytes(), original)
        self.assertEqual(list(Path(result["dataDirectory"]).iterdir()), [])

    def test_two_profiles_have_distinct_paths_and_bundle_ids(self):
        a, b = self.create("Work"), self.create("Personal")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["dataDirectory"], b["dataDirectory"])
        self.assertNotEqual(a["codeConfigDirectory"], b["codeConfigDirectory"])
        self.assertNotEqual(profiles.app_info(a)["CFBundleIdentifier"], profiles.app_info(b)["CFBundleIdentifier"])

    def test_proxy_opt_in_is_seeded_in_companion_not_cookie_directory(self):
        self.proxy.return_value = True
        result = self.create()
        companion = Path(result["dataDirectory"] + "-3p")
        meta = profiles.read_json(companion / "configLibrary/_meta.json")
        config = profiles.read_json(companion / "configLibrary" / (meta["appliedId"] + ".json"))
        self.assertEqual(config, {profiles.ENABLE_PROXY: True})
        self.assertNotIn("inferenceProvider", config)
        self.assertFalse((Path(result["dataDirectory"]) / "configLibrary").exists())

    def test_shared_config_is_explicit_and_does_not_copy_desktop_data(self):
        result = self.create(shared=True)
        self.assertTrue(result["sharedCodeConfig"])
        self.assertEqual(result["codeConfigDirectory"], str(Path.home() / ".claude"))
        self.assertEqual(list(Path(result["dataDirectory"]).iterdir()), [])

    def test_proxy_off_does_not_read_global_policy(self):
        self.proxy.side_effect = AssertionError("Must not inspect policy")
        result = self.create(code_proxy="off")
        self.assertFalse(result["codeProxyEnabled"])

    def test_proxy_on_requires_installation(self):
        with self.assertRaisesRegex(ValueError, "Install"):
            self.create(code_proxy="on")
        self.assertFalse((self.root / "_profiles.json").exists())

    def test_duplicate_names_are_rejected_case_insensitively(self):
        self.create()
        with self.assertRaisesRegex(ValueError, "name already"):
            self.create("claude work")
        self.assertEqual(len(profiles.registry(self.root)["profiles"]), 1)

    def test_existing_destination_is_not_overwritten(self):
        destination = self.apps / "Claude Work.app"
        destination.mkdir(parents=True)
        marker = destination / "keep"
        marker.write_text("untouched")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.create()
        self.assertEqual(marker.read_text(), "untouched")

    def test_bad_names_and_colours_are_rejected(self):
        for value in ("", "..", "../escape", "a/b", "a:b", "a\nname", ".hidden"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profiles.valid_name(value)
        self.assertEqual(profiles.valid_name("  Claude Blue.app  "), "Claude Blue")
        self.assertEqual(profiles.color_value("#abcdef"), "#ABCDEF")
        with self.assertRaises(ValueError):
            profiles.color_value("not-a-colour")

    def test_output_inside_vendor_bundle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            profiles.ensure_external(self.source, self.source / "data", self.apps)
        with self.assertRaisesRegex(ValueError, "outside"):
            profiles.ensure_external(self.source, self.root, self.source / "copies")

    def test_seed_does_not_overwrite_existing_library(self):
        companion = self.base / "companion"
        (companion / "configLibrary").mkdir(parents=True)
        marker = companion / "configLibrary/keep"
        marker.write_text("existing")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            profiles.seed_proxy(companion)
        self.assertEqual(marker.read_text(), "existing")

    def test_failed_registry_commit_cleans_only_new_outputs(self):
        original = profiles.write_json
        def failure(path, value):
            if path == self.root / "_profiles.json":
                raise OSError("test commit failure")
            return original(path, value)
        with mock.patch.object(profiles, "write_json", side_effect=failure):
            with self.assertRaises(OSError):
                self.create()
        self.assertFalse((self.apps / "Claude Work.app").exists())
        self.assertEqual([p.name for p in self.root.iterdir()], [".registry.lock"])
        self.assertTrue((self.source / "Contents/Info.plist").exists())

    def test_manifest_changes_prevent_launch_or_removal(self):
        profile = self.create()
        manifest = Path(profile["appPath"]) / "Contents/Resources/profile.json"
        changed = profiles.read_json(manifest)
        changed["sourceApp"] = "/different/source.app"
        profiles.write_json(manifest, changed)
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            profiles.checked_bundle(profile)

    def test_running_profile_is_not_removed(self):
        profile = self.create()
        with mock.patch.object(profiles, "running_profiles", return_value={profile["id"]: [100]}):
            with self.assertRaisesRegex(ValueError, "running"):
                profiles.remove_profile(profile["name"], self.root)
        self.assertTrue(Path(profile["appPath"]).exists())

    def test_removal_keeps_account_and_code_data(self):
        profile = self.create()
        marker = Path(profile["dataDirectory"]) / "keep-account-data"
        marker.write_text("private fixture")
        with mock.patch.object(profiles, "running_profiles", return_value={profile["id"]: []}):
            profiles.remove_profile(profile["name"], self.root)
        self.assertFalse(Path(profile["appPath"]).exists())
        self.assertEqual(marker.read_text(), "private fixture")
        self.assertTrue(Path(profile["codeConfigDirectory"]).is_dir())
        self.assertEqual(profiles.registry(self.root)["profiles"], [])

    def test_failed_removal_registry_commit_restores_launcher(self):
        profile = self.create()
        with mock.patch.object(profiles, "running_profiles", return_value={profile["id"]: []}), \
                mock.patch.object(profiles, "write_json", side_effect=OSError("test commit failure")):
            with self.assertRaises(OSError):
                profiles.remove_profile(profile["name"], self.root)
        self.assertTrue(Path(profile["appPath"]).exists())
        self.assertEqual(len(profiles.registry(self.root)["profiles"]), 1)

    def test_lock_rejects_concurrent_writes(self):
        with profiles.registry_lock(self.root):
            with self.assertRaisesRegex(ValueError, "in progress"):
                with profiles.registry_lock(self.root):
                    self.fail("Lock acquired twice")

    def test_profile_pid_detection_uses_exact_argument(self):
        profile = self.create()
        process_list = "100 /Applications/Claude.app/Contents/MacOS/Claude\n101 /usr/bin/node\n"
        command = "Claude --user-data-dir=" + profile["dataDirectory"] + "-other"
        with mock.patch.object(profiles.subprocess, "check_output", return_value=process_list), \
                mock.patch.object(profiles.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=command)):
            self.assertEqual(profiles.running_profiles([profile])[profile["id"]], [])
        command = "Claude --user-data-dir=" + profile["dataDirectory"] + " --another-option"
        with mock.patch.object(profiles.subprocess, "check_output", return_value=process_list), \
                mock.patch.object(profiles.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=command)):
            self.assertEqual(profiles.running_profiles([profile])[profile["id"]], [100])

    def test_edited_or_duplicate_registry_is_rejected(self):
        profile = self.create()
        profiles.write_json(self.root / "_profiles.json", {"schemaVersion": 1, "profiles": [profile, profile]})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            profiles.registry(self.root)

    def test_svg_recolors_background_without_changing_foreground_or_geometry(self):
        source = self.svg_fixture()
        original = profiles.ET.fromstring(source.read_bytes())
        output = self.base / "output.svg"
        profiles.recolor_svg(source, output, "purple")
        result = profiles.ET.fromstring(output.read_bytes())
        self.assertEqual(result.get("viewBox"), original.get("viewBox"))
        self.assertEqual(result.get("width"), "1024")
        self.assertEqual(result[0].get("fill"), profiles.COLORS["purple"])
        self.assertEqual(result[1].get("fill"), "#FCF2EE")
        self.assertEqual([p.get("d") for p in result], [p.get("d") for p in original])
        self.assertEqual(profiles.ET.fromstring(source.read_bytes())[0].get("fill"), "#D77655")

    def test_svg_rejects_active_content_and_external_resources(self):
        source = self.svg_fixture()
        original = source.read_text()
        variants = [
            '<!DOCTYPE svg [<!ENTITY test "content">]>' + original,
            original.replace('<path ', '<script/> <path ', 1),
            original.replace('<path ', '<style/> <path ', 1),
            original.replace('<path ', '<image href="https://example.com/image.png"/> <path ', 1),
            original.replace('<path ', '<path onload="alert(1)" ', 1),
            original.replace('<path ', '<path style="fill:url(https://example.com/)" ', 1),
            original.replace('<path ', '<path transform="scale(2)" ', 1),
            original.replace('#D77655', 'url(https://example.com/fill)'),
            original.replace('0 0 512 509.64', '0 0 nan 512'),
            original.replace('0 0 512 509.64', '0 0 0 512'),
            'x' * 1_048_577,
        ]
        for payload in variants:
            with self.subTest(payload=payload[:80]):
                source.write_text(payload)
                output = self.base / "rejected.svg"
                with self.assertRaises(ValueError):
                    profiles.recolor_svg(source, output, "black")
                self.assertFalse(output.exists())

    def test_svg_rejects_non_utf8_entity_documents(self):
        source = self.svg_fixture()
        source.write_bytes(('<!DOCTYPE svg [<!ENTITY test "content">]>' + source.read_text()).encode("utf-16"))
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            profiles.recolor_svg(source, self.base / "output.svg", "black")

    def test_icon_update_and_relocation_preserve_profile_and_data(self):
        self.proxy.return_value = True
        original = self.create()
        marker = Path(original["dataDirectory"]) / "account-marker"
        marker.write_text("private fixture")
        code_marker = Path(original["codeConfigDirectory"]) / "settings.json"
        code_marker.write_text('{}')
        other = self.create("Other")
        updated = profiles.update_icon(original["id"], self.root, "black", self.svg_fixture(), self.base / "NewApps")
        for key in ("id", "name", "sourceApp", "dataDirectory", "codeConfigDirectory", "sharedCodeConfig", "codeProxyEnabled"):
            self.assertEqual(updated[key], original[key])
        self.assertEqual(updated["color"], "#000000")
        self.assertEqual(updated["iconStyle"], "svg")
        self.assertEqual(profiles.app_info(updated)["CFBundleVersion"], "2")
        self.assertFalse(Path(original["appPath"]).exists())
        self.assertTrue(profiles.checked_bundle(updated).is_dir())
        self.assertEqual(marker.read_text(), "private fixture")
        self.assertEqual(code_marker.read_text(), '{}')
        self.assertTrue(profiles.checked_bundle(other).is_dir())
        self.assertEqual(profiles.registry(self.root)["profiles"], [updated, other])
        self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])

    def test_svg_icon_can_be_recolored_from_embedded_source(self):
        original = self.create(icon_svg=self.svg_fixture())
        updated = profiles.update_icon(original["name"], self.root, "purple")
        self.assertEqual(updated["appPath"], original["appPath"])
        self.assertEqual(updated["iconStyle"], "svg")
        icon = profiles.ET.fromstring((Path(updated["appPath"]) / "Contents/Resources/icon.svg").read_bytes())
        self.assertEqual(icon[0].get("fill"), profiles.COLORS["purple"])
        self.assertEqual(icon[1].get("fill"), "#FCF2EE")

    def test_icon_update_refuses_existing_destination_or_symlink(self):
        original = self.create()
        apps = self.base / "NewApps"
        apps.mkdir()
        destination = apps / (original["name"] + ".app")
        destination.mkdir()
        marker = destination / "keep"
        marker.write_text("untouched")
        with self.assertRaisesRegex(ValueError, "already exists"):
            profiles.update_icon(original["id"], self.root, apps=apps)
        self.assertEqual(marker.read_text(), "untouched")
        marker.unlink()
        destination.rmdir()
        destination.symlink_to(self.base / "nonexistent.app")
        with self.assertRaisesRegex(ValueError, "symlink"):
            profiles.update_icon(original["id"], self.root, apps=apps)
        self.assertTrue(destination.is_symlink())
        self.assertTrue(profiles.checked_bundle(original).is_dir())

    def test_icon_update_rolls_back_registry_failure(self):
        original = self.create()
        real_write = profiles.write_json
        def failure(path, value):
            if path == self.root / "_profiles.json":
                raise OSError("test commit failure")
            return real_write(path, value)
        apps = self.base / "NewApps"
        with mock.patch.object(profiles, "write_json", side_effect=failure):
            with self.assertRaises(OSError):
                profiles.update_icon(original["id"], self.root, "black", self.svg_fixture(), apps)
        self.assertTrue(profiles.checked_bundle(original).is_dir())
        self.assertEqual(profiles.registry(self.root)["profiles"], [original])
        self.assertFalse((apps / (original["name"] + ".app")).exists())
        self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])

    def test_icon_build_failure_leaves_original_untouched(self):
        original = self.create()
        with mock.patch.object(profiles, "build_bundle", side_effect=ValueError("build failed")):
            with self.assertRaisesRegex(ValueError, "build failed"):
                profiles.update_icon(original["id"], self.root, "black")
        self.assertTrue(profiles.checked_bundle(original).is_dir())
        self.assertEqual(profiles.registry(self.root)["profiles"], [original])

    def test_cli_wires_svg_creation_and_icon_relocation(self):
        source = self.svg_fixture()
        with mock.patch("builtins.print"):
            profiles.main(["create", "CLI profile", "--root", str(self.root), "--source", str(self.source),
                           "--apps-dir", str(self.apps), "--color", "black", "--icon-svg", str(source)])
            profiles.main(["set-icon", "CLI profile", "--root", str(self.root), "--color", "purple",
                           "--apps-dir", str(self.base / "NewApps")])
        updated = profiles.registry(self.root)["profiles"][0]
        self.assertEqual(updated["iconStyle"], "svg")
        self.assertEqual(Path(updated["appPath"]).parent, (self.base / "NewApps").resolve())
        self.assertEqual(updated["color"], profiles.COLORS["purple"])
        self.assertEqual(profiles.DEFAULT_APPS, Path("/Applications"))

    def set_source_metadata(self, **changes):
        path = self.source / "Contents/Info.plist"
        info = {**plistlib.loads(path.read_bytes()), **changes}
        path.write_bytes(plistlib.dumps(info))
        return info

    def test_mode_metadata_refreshes_copy_and_does_not_mutate_input(self):
        original = {"name": "Work", "mode": "copy", "runtimeExecutable": "Old.real",
                    "sourceVersion": "old", "sourceBuildVersion": "old-build"}
        info = {"CFBundleExecutable": "NativeClaude", "CFBundleShortVersionString": "4.0",
                "CFBundleVersion": "400"}
        updated = profiles.mode_metadata(original, info)
        self.assertEqual(updated, {**original, "runtimeExecutable": "NativeClaude.real",
                                  "sourceVersion": "4.0", "sourceBuildVersion": "400"})
        self.assertEqual(original["runtimeExecutable"], "Old.real")
        self.assertEqual(original["sourceVersion"], "old")
        self.assertEqual(original["sourceBuildVersion"], "old-build")
        self.assertEqual(profiles.mode_metadata({"name": "Work"}, {}, "copy"),
                         {"name": "Work", "mode": "copy", "runtimeExecutable": "Claude.real",
                          "sourceVersion": "unknown", "sourceBuildVersion": "unknown"})

    def test_mode_metadata_launcher_removes_copy_fields_and_rejects_unknown_mode(self):
        original = {"name": "Work", "mode": "copy", "runtimeExecutable": "Claude.real",
                    "sourceVersion": "4.0", "sourceBuildVersion": "400", "color": "#000000"}
        self.assertEqual(profiles.mode_metadata(original, {}, "launcher"),
                         {"name": "Work", "mode": "launcher", "color": "#000000"})
        self.assertIn("runtimeExecutable", original)
        self.assertEqual(profiles.mode_metadata({"name": "Work"}, {}),
                         {"name": "Work", "mode": "launcher"})
        with self.assertRaisesRegex(ValueError, "launcher or copy"):
            profiles.mode_metadata(original, {}, "unsupported")

    def test_copy_creation_records_runtime_and_source_versions(self):
        self.set_source_metadata(CFBundleVersion="365562")
        source_before = (self.source / "Contents/Info.plist").read_bytes()
        profile = self.create(mode="copy")
        self.assertEqual(profile["mode"], "copy")
        self.assertEqual(profile["runtimeExecutable"], "Claude.real")
        self.assertEqual(profile["sourceVersion"], "3.6556.2")
        self.assertEqual(profile["sourceBuildVersion"], "365562")
        self.assertEqual(profiles.registry(self.root)["profiles"], [profile])
        self.assertTrue(profiles.checked_bundle(profile).is_dir())
        self.assertEqual((self.source / "Contents/Info.plist").read_bytes(), source_before)
        self.assertFalse((self.source / "Contents/MacOS/Claude.real").exists())

    def test_conversion_and_refresh_preserve_identity_data_proxy_and_embedded_svg(self):
        self.proxy.return_value = True
        self.set_source_metadata(CFBundleVersion="365562")
        svg = self.svg_fixture()
        original = self.create(icon_svg=svg)
        svg.unlink()  # Refresh must use the embedded source, not the original input path.
        markers = [Path(original["dataDirectory"]) / "account",
                   Path(original["codeConfigDirectory"]) / "settings.json",
                   Path(original["dataDirectory"] + "-3p") / "proxy-marker"]
        for marker in markers:
            marker.write_text("keep " + marker.name)
        directory_inodes = {marker.parent: marker.parent.stat().st_ino for marker in markers}
        proxy_meta = profiles.read_json(Path(original["dataDirectory"] + "-3p") / "configLibrary/_meta.json")
        with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: []}), \
                mock.patch.object(profiles, "seed_proxy") as seed:
            converted = profiles.update_icon(original["id"], self.root, mode="copy")
            self.assertEqual(converted["sourceVersion"], "3.6556.2")
            self.assertEqual(converted["sourceBuildVersion"], "365562")
            self.set_source_metadata(CFBundleShortVersionString="4.0.1", CFBundleVersion="4001")
            refreshed = profiles.update_icon(original["name"], self.root)
            seed.assert_not_called()
        self.assertEqual(refreshed["mode"], "copy")
        self.assertEqual(refreshed["runtimeExecutable"], "Claude.real")
        self.assertEqual(refreshed["sourceVersion"], "4.0.1")
        self.assertEqual(refreshed["sourceBuildVersion"], "4001")
        self.assertEqual(refreshed["launcherRevision"], 3)
        for key in ("id", "name", "sourceApp", "dataDirectory", "codeConfigDirectory", "appPath",
                    "sharedCodeConfig", "codeProxyEnabled", "iconStyle", "color"):
            self.assertEqual(refreshed[key], original[key], key)
        self.assertTrue(refreshed["codeProxyEnabled"])
        for marker in markers:
            self.assertEqual(marker.read_text(), "keep " + marker.name)
            self.assertEqual(marker.parent.stat().st_ino, directory_inodes[marker.parent])
        self.assertEqual(profiles.read_json(Path(original["dataDirectory"] + "-3p") / "configLibrary/_meta.json"),
                         proxy_meta)
        icon = profiles.ET.fromstring((Path(refreshed["appPath"]) / "Contents/Resources/icon.svg").read_bytes())
        self.assertEqual(icon[0].get("fill"), original["color"])
        self.assertEqual(icon[1].get("fill"), "#FCF2EE")
        self.assertEqual(icon.get("viewBox"), "0 0 512 509.64")
        self.assertTrue(profiles.checked_bundle(refreshed).is_dir())
        self.assertEqual(profiles.registry(self.root)["profiles"], [refreshed])
        self.assertEqual(sorted(path.name for path in self.apps.iterdir()), ["Claude Work.app"])

    def test_copy_can_switch_back_to_launcher_without_copy_metadata(self):
        original = self.create(mode="copy", shared=True, icon_svg=self.svg_fixture())
        marker = Path(original["dataDirectory"]) / "account"
        marker.write_text("keep account")
        with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: []}):
            updated = profiles.update_icon(original["id"], self.root, "purple", mode="launcher")
        self.assertEqual(updated["mode"], "launcher")
        for key in ("runtimeExecutable", "sourceVersion", "sourceBuildVersion"):
            self.assertNotIn(key, updated)
        for key in ("id", "dataDirectory", "codeConfigDirectory", "sharedCodeConfig", "codeProxyEnabled", "appPath"):
            self.assertEqual(updated[key], original[key], key)
        self.assertEqual(updated["iconStyle"], "svg")
        self.assertEqual(marker.read_text(), "keep account")
        self.assertTrue(profiles.checked_bundle(updated).is_dir())
        self.assertEqual(profiles.app_info(updated)["CFBundleExecutable"], "ProfileLauncher")
        self.assertEqual(profiles.registry(self.root)["profiles"], [updated])

    def test_copy_related_updates_refuse_running_profiles_before_building(self):
        for old_mode, new_mode in (("launcher", "copy"), ("copy", None),
                                   ("copy", "copy"), ("copy", "launcher")):
            with self.subTest(old_mode=old_mode, new_mode=new_mode):
                original = self.create("Running " + old_mode + " " + str(new_mode), mode=old_mode)
                registry_before = (self.root / "_profiles.json").read_bytes()
                manifest = Path(original["appPath"]) / "Contents/Resources/profile.json"
                manifest_before = manifest.read_bytes()
                profiles.build_bundle.reset_mock()
                profiles.build_launcher.reset_mock()
                with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: [123]}) as running:
                    with self.assertRaisesRegex(ValueError, "running.*nothing was stopped"):
                        profiles.update_icon(original["id"], self.root, "purple", mode=new_mode)
                running.assert_called_once_with([original])
                profiles.build_bundle.assert_not_called()
                profiles.build_launcher.assert_not_called()
                self.assertEqual(manifest.read_bytes(), manifest_before)
                self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)
                self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])

    def test_copy_update_build_and_registry_failures_restore_original(self):
        for old_mode, failure_stage in (("launcher", "build"), ("copy", "build"),
                                        ("launcher", "registry"), ("copy", "registry")):
            with self.subTest(old_mode=old_mode, failure_stage=failure_stage):
                original = self.create("Rollback " + old_mode + " " + failure_stage,
                                       mode=old_mode, icon_svg=self.svg_fixture())
                bundle = Path(original["appPath"])
                manifest_before = (bundle / "Contents/Resources/profile.json").read_bytes()
                svg_before = (bundle / "Contents/Resources/icon.svg").read_bytes()
                registry_before = (self.root / "_profiles.json").read_bytes()
                marker = Path(original["dataDirectory"]) / "account"
                marker.write_text("keep account")
                real_write = profiles.write_json

                def fail_registry(path, value):
                    if path == self.root / "_profiles.json":
                        raise OSError("copy registry failure")
                    return real_write(path, value)

                failure = (mock.patch.object(profiles, "build_bundle", side_effect=ValueError("copy build failure"))
                           if failure_stage == "build" else
                           mock.patch.object(profiles, "write_json", side_effect=fail_registry))
                apps = self.base / "RollbackApps"
                with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: []}), failure:
                    with self.assertRaisesRegex((ValueError, OSError), "copy .* failure"):
                        profiles.update_icon(original["id"], self.root, "black", apps=apps, mode="copy")
                self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)
                self.assertEqual((bundle / "Contents/Resources/profile.json").read_bytes(), manifest_before)
                self.assertEqual((bundle / "Contents/Resources/icon.svg").read_bytes(), svg_before)
                self.assertEqual(marker.read_text(), "keep account")
                self.assertTrue(profiles.checked_bundle(original).is_dir())
                self.assertEqual(list(apps.iterdir()), [])
                self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])

    def test_copy_update_publication_failure_restores_original_in_place(self):
        original = self.create(mode="copy")
        registry_before = (self.root / "_profiles.json").read_bytes()
        real_rename = profiles.os.rename

        def fail_publication(source, destination):
            if Path(source).parent.name.startswith(".claude-icon-build-"):
                raise OSError("copy publication failure")
            return real_rename(source, destination)

        with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: []}), \
                mock.patch.object(profiles.os, "rename", side_effect=fail_publication):
            with self.assertRaisesRegex(OSError, "copy publication failure"):
                profiles.update_icon(original["id"], self.root)
        self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)
        self.assertTrue(profiles.checked_bundle(original).is_dir())
        self.assertEqual(sorted(path.name for path in self.apps.iterdir()), ["Claude Work.app"])

    def test_copy_update_refuses_profile_started_during_build_without_touching_original(self):
        for old_mode, new_mode in (("launcher", "copy"), ("copy", None), ("copy", "launcher")):
            with self.subTest(old_mode=old_mode, new_mode=new_mode):
                original = self.create("Started " + old_mode + " " + str(new_mode),
                                       mode=old_mode, icon_svg=self.svg_fixture())
                bundle = Path(original["appPath"])
                bundle_inode = bundle.stat().st_ino
                manifest_before = (bundle / "Contents/Resources/profile.json").read_bytes()
                svg_before = (bundle / "Contents/Resources/icon.svg").read_bytes()
                registry_before = (self.root / "_profiles.json").read_bytes()
                marker = Path(original["dataDirectory"]) / "account"
                marker.write_text("keep account")
                profiles.build_bundle.reset_mock()
                with mock.patch.object(profiles, "running_profiles", side_effect=
                        [{original["id"]: []}, {original["id"]: [456]}]) as running, \
                        mock.patch.object(profiles.os, "rename") as rename:
                    with self.assertRaisesRegex(ValueError, "started during the build.*nothing was stopped"):
                        profiles.update_icon(original["id"], self.root, "black", mode=new_mode)
                self.assertEqual(running.call_args_list, [mock.call([original]), mock.call([original])])
                profiles.build_bundle.assert_called_once()
                rename.assert_not_called()
                self.assertEqual(bundle.stat().st_ino, bundle_inode)
                self.assertEqual((bundle / "Contents/Resources/profile.json").read_bytes(), manifest_before)
                self.assertEqual((bundle / "Contents/Resources/icon.svg").read_bytes(), svg_before)
                self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)
                self.assertEqual(marker.read_text(), "keep account")
                self.assertTrue(profiles.checked_bundle(original).is_dir())
                self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])
                self.assertEqual(list(self.apps.glob(".claude-icon-build-*")), [])

    def test_copy_update_rechecks_original_manifest_before_publication(self):
        original = self.create(mode="copy")
        manifest = Path(original["appPath"]) / "Contents/Resources/profile.json"
        registry_before = (self.root / "_profiles.json").read_bytes()
        changed = {**original, "color": "#000000"}

        def changed_during_build(*args, **kwargs):
            bundle = self.fake_bundle(*args, **kwargs)
            profiles.write_json(manifest, changed)
            return bundle

        with mock.patch.object(profiles, "running_profiles", return_value={original["id"]: []}), \
                mock.patch.object(profiles, "build_bundle", side_effect=changed_during_build), \
                mock.patch.object(profiles.os, "rename") as rename:
            with self.assertRaisesRegex(ValueError, "manifest no longer matches"):
                profiles.update_icon(original["id"], self.root)
        rename.assert_not_called()
        self.assertEqual(profiles.read_json(manifest), changed)
        self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)
        self.assertEqual(list(self.apps.glob(".icon-backup-*")), [])
        self.assertEqual(list(self.apps.glob(".claude-icon-build-*")), [])

    def test_profile_pid_detection_includes_renamed_runtime_and_ignores_lookalikes(self):
        first, second = self.create(mode="copy"), self.create("Other", mode="copy")
        process_list = ("100 /Applications/Claude.app/Contents/MacOS/Claude\n"
                        "101 /Applications/Claude Work.app/Contents/MacOS/Claude.real\n"
                        "102 /Applications/Other.app/Contents/MacOS/Claude.real\n"
                        "103 /Applications/Claude.app/Contents/MacOS/Claude.real-other\n"
                        "104 /Applications/Claude.app/Contents/MacOS/Claude Helper\n")
        commands = {"100": "Claude --user-data-dir=" + first["dataDirectory"],
                    "101": "Claude.real --user-data-dir=" + first["dataDirectory"] + " --flag",
                    "102": "Claude.real --user-data-dir=" + second["dataDirectory"] + "-other"}

        def process_command(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=commands[command[3]])

        with mock.patch.object(profiles.subprocess, "check_output", return_value=process_list), \
                mock.patch.object(profiles.subprocess, "run", side_effect=process_command) as inspect:
            self.assertEqual(profiles.running_profiles([first, second]),
                             {first["id"]: [100, 101], second["id"]: []})
        self.assertEqual([call.args[0][3] for call in inspect.call_args_list], ["100", "101", "102"])

    def test_signing_entitlements_requests_xml_and_filters_protected_vendor_claims(self):
        ordinary = {"com.apple.security.cs.allow-jit": True,
                    "com.apple.security.cs.allow-unsigned-executable-memory": True,
                    "com.apple.security.network.client": True,
                    "com.apple.security.get-task-allow": False}
        protected = {"com.apple.developer.team-identifier": "VENDORTEAM",
                     "com.apple.developer.associated-domains": ["applinks:example.com"],
                     "com.apple.application-identifier": "VENDORTEAM.vendor.app",
                     "application-identifier": "VENDORTEAM.vendor.app",
                     "keychain-access-groups": ["VENDORTEAM.vendor.keychain"],
                     "com.apple.security.application-groups": ["group.vendor.app"]}
        executable = self.source / "Contents/MacOS/Claude"
        result = subprocess.CompletedProcess([], 0, stdout=plistlib.dumps({**ordinary, **protected}))
        with mock.patch.object(profiles.subprocess, "run", return_value=result) as inspect:
            self.assertEqual(profiles.signing_entitlements(executable), ordinary)
        inspect.assert_called_once_with(["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(executable)],
                                        capture_output=True)

    def test_signing_entitlements_handles_unsigned_metadata_and_inspection_failure(self):
        for output in (b"", b" \n", plistlib.dumps({})):
            with self.subTest(output=output), mock.patch.object(profiles.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, stdout=output)):
                self.assertEqual(profiles.signing_entitlements(self.source), {})
        with mock.patch.object(profiles.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"inspection failed")):
            with self.assertRaisesRegex(ValueError, "Cannot inspect signing entitlements"):
                profiles.signing_entitlements(self.source)

    def test_copy_signing_is_deepest_first_and_uses_deep_only_for_verification(self):
        staging = self.base / "signing"
        bundle = staging / "Copy.app"
        nested = [bundle / "Contents/Frameworks/Electron.framework",
                  bundle / "Contents/Helpers/Helper.app",
                  bundle / "Contents/Helpers/Helper.app/Contents/XPCServices/Service.xpc",
                  bundle / "Contents/PlugIns/Extension.appex",
                  bundle / "Contents/Resources/Plugin.bundle"]
        for path in nested:
            path.mkdir(parents=True)
        binaries = [bundle / "Contents/MacOS/Claude", bundle / "Contents/MacOS/Claude.real",
                    nested[0] / "Versions/A/Electron", nested[0] / "Versions/A/Libraries/libfixture.dylib",
                    nested[2] / "Contents/MacOS/Service", nested[4] / "module.node"]
        for index, path in enumerate(binaries):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((b"\xcf\xfa\xed\xfe" if index % 2 else b"\xca\xfe\xba\xbe") + b"fixture")
            path.chmod(0o644 if path.suffix in (".dylib", ".node") else 0o755)
        shell = bundle / "Contents/MacOS/script"
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)
        (bundle / "Contents/Resources/not-executable").write_bytes(b"\xcf\xfa\xed\xfe" + b"data")
        (nested[0] / "Versions/Current").symlink_to("A", target_is_directory=True)
        (bundle / "Contents/MacOS/alias").symlink_to(binaries[0])
        commands, captured_entitlements = [], {}
        entitlement = {"com.apple.security.cs.allow-jit": True}

        def inspect(path):
            return entitlement if path in (binaries[1], bundle) else {}

        def sign(command):
            commands.append(command)
            if "--entitlements" in command:
                captured_entitlements[command[-1]] = plistlib.loads(Path(command[command.index("--entitlements") + 1]).read_bytes())
            return ""

        with mock.patch.object(profiles, "signing_entitlements", side_effect=inspect) as inspection, \
                mock.patch.object(profiles, "run", side_effect=sign):
            profiles.sign_copy(bundle, staging)
        signing = commands[:-1]
        targets = [command[-1] for command in signing]
        self.assertCountEqual(targets, [*nested, *binaries, bundle])
        self.assertEqual(targets[-1], bundle)
        depths = [len(path.parts) for path in targets[:-1]]
        self.assertEqual(depths, sorted(depths, reverse=True))
        self.assertEqual([call.args[0] for call in inspection.call_args_list], targets)
        for command in signing:
            self.assertEqual(command[:5], ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none"])
            self.assertNotIn("--deep", command)
        self.assertEqual(captured_entitlements, {binaries[1]: entitlement, bundle: entitlement})
        self.assertEqual(commands[-1], ["/usr/bin/codesign", "--verify", "--deep", "--strict", bundle])

    def helper_fixtures(self, bundle, source_name="Claude"):
        helpers = {}
        for suffix, tag in (("", "default"), (" (GPU)", "gpu"), (" (Renderer)", "renderer"), (" (Plugin)", "plugin")):
            name = source_name + " Helper" + suffix
            helper = bundle / "Contents/Frameworks" / (name + ".app")
            (helper / "Contents/MacOS").mkdir(parents=True)
            (helper / "Contents/MacOS" / name).write_bytes(b"synthetic helper " + tag.encode())
            (helper / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": name, "CFBundleName": name, "CFBundleIdentifier": "vendor.helper." + tag,
                "LSUIElement": True, "CFBundleVersion": "123",
            }))
            helpers[suffix] = tag
        return helpers

    def test_copy_helpers_are_renamed_with_profile_identity_and_keep_ui_flags(self):
        fixture = self.base / "Helpers.app"
        helpers = self.helper_fixtures(fixture, "SourceClaude")
        profile = {"name": "Claude Work", "id": str(uuid.uuid4())}
        profiles.rename_helpers(fixture, profile, "SourceClaude")
        for suffix, tag in helpers.items():
            name = profile["name"] + " Helper" + suffix
            helper = fixture / "Contents/Frameworks" / (name + ".app")
            info = plistlib.loads((helper / "Contents/Info.plist").read_bytes())
            for key in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
                self.assertEqual(info[key], name)
            self.assertEqual(info["CFBundleIdentifier"], profiles.app_info(profile)["CFBundleIdentifier"] + ".helper." + tag)
            self.assertTrue(info["LSUIElement"])
            self.assertEqual(info["CFBundleVersion"], "123")
            self.assertEqual((helper / "Contents/MacOS" / name).read_bytes(), b"synthetic helper " + tag.encode())
            self.assertFalse((fixture / "Contents/Frameworks" / ("SourceClaude Helper" + suffix + ".app")).exists())

    def test_copy_helpers_reject_missing_symlinked_or_unsafe_layout(self):
        profile = {"name": "Work", "id": str(uuid.uuid4())}
        missing = self.base / "Missing.app"
        with self.assertRaisesRegex(ValueError, "helper layout"):
            profiles.rename_helpers(missing, profile, "Claude")
        for invalid in ("symlink", "executable"):
            with self.subTest(invalid=invalid):
                bundle = self.base / (invalid + ".app")
                self.helper_fixtures(bundle)
                helper = bundle / "Contents/Frameworks/Claude Helper.app"
                if invalid == "symlink":
                    target = self.base / "HelperTarget.app"
                    helper.rename(target)
                    helper.symlink_to(target, target_is_directory=True)
                    message = "helper layout"
                else:
                    info_path = helper / "Contents/Info.plist"
                    info = plistlib.loads(info_path.read_bytes())
                    info["CFBundleExecutable"] = "../outside"
                    info_path.write_bytes(plistlib.dumps(info))
                    message = "helper executable path"
                with self.assertRaisesRegex(ValueError, message):
                    profiles.rename_helpers(bundle, profile, "Claude")

    def test_runtime_copy_bundle_wraps_native_binary_and_updates_metadata_with_mocked_tools(self):
        self.set_source_metadata(CFBundleVersion="365562", CFBundleIconName="VendorIcon", ElectronTeamID="VENDORTEAM",
                                 LSUIElement=True, LSBackgroundOnly=True, VendorSetting="keep")
        self.helper_fixtures(self.source)
        resources = self.source / "Contents/Resources"
        resources.mkdir()
        source_icon = resources / "electron.icns"
        source_icon.write_bytes(b"vendor icon")
        source_plist = (self.source / "Contents/Info.plist").read_bytes()
        source_executable = (self.source / "Contents/MacOS/Claude").read_bytes()
        launcher = self.base / "launcher"
        launcher.write_bytes(b"synthetic launcher")
        svg = self.svg_fixture()
        profile = self.create(mode="copy", icon_svg=svg)
        for clone_succeeds in (True, False):
            with self.subTest(clone_succeeds=clone_succeeds):
                staging = self.base / ("cloned" if clone_succeeds else "fallback")
                staging.mkdir()
                expected = staging / (profile["name"] + ".app")
                commands = []

                def clone(command, **kwargs):
                    self.assertEqual(command, ["/bin/cp", "-cR", str(self.source.resolve()), str(expected)])
                    if clone_succeeds:
                        shutil.copytree(self.source, expected)
                    else:
                        expected.mkdir()
                        (expected / "partial-clone").write_text("must be removed before ditto")
                    return subprocess.CompletedProcess(command, 0 if clone_succeeds else 1)

                def native_tool(command):
                    commands.append(command)
                    if command[0] == "/usr/bin/ditto":
                        self.assertFalse(expected.exists())
                        shutil.copytree(command[1], command[2])
                    elif command[0] == "/usr/bin/codesign" and command[-1] == expected:
                        self.assertEqual((expected / "Contents/Info.plist").read_bytes(), source_plist)
                        self.assertEqual((expected / "Contents/MacOS/Claude").read_bytes(), source_executable)
                        self.assertFalse((expected / "Contents/MacOS/Claude.real").exists())
                        self.assertFalse((expected / "Contents/Resources/profile.json").exists())
                    elif command[0] == "/usr/bin/iconutil":
                        Path(command[command.index("--output") + 1]).write_bytes(b"profile icon fixture")
                    return ""

                with mock.patch.object(profiles.subprocess, "run", side_effect=clone) as copy, \
                        mock.patch.object(profiles, "run", side_effect=native_tool), \
                        mock.patch.object(profiles, "sign_copy") as sign:
                    result = self.real_build_bundle(profile, launcher, source_icon, staging, svg)
                self.assertEqual(result, expected)
                copy.assert_called_once()
                sign.assert_called_once_with(expected, staging)
                self.assertEqual([command for command in commands if command[0] == "/usr/bin/codesign"], [
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", self.source.resolve()],
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", expected],
                ])
                self.assertEqual([command for command in commands if command[0] == "/usr/bin/ditto"],
                                 [] if clone_succeeds else [["/usr/bin/ditto", self.source.resolve(), expected]])
                info = plistlib.loads((expected / "Contents/Info.plist").read_bytes())
                self.assertEqual(info["CFBundleIdentifier"], profiles.app_info(profile)["CFBundleIdentifier"])
                self.assertEqual(info["CFBundleName"], profile["name"])
                self.assertEqual(info["CFBundleDisplayName"], profile["name"])
                self.assertEqual(info["CFBundleExecutable"], "Claude")
                self.assertEqual(info["CFBundleIconFile"], "profile.icns")
                self.assertTrue(info["MultiClaudeRuntimeCopy"])
                self.assertEqual(info["CFBundleShortVersionString"], profile["sourceVersion"])
                self.assertEqual(info["CFBundleVersion"], profile["sourceBuildVersion"])
                self.assertEqual(info["VendorSetting"], "keep")
                for key in ("CFBundleIconName", "LSUIElement", "LSBackgroundOnly", "ElectronTeamID"):
                    self.assertNotIn(key, info)
                self.assertTrue((expected / "Contents/Frameworks/Claude Work Helper.app/Contents/MacOS/Claude Work Helper").is_file())
                self.assertFalse((expected / "Contents/Frameworks/Claude Helper.app").exists())
                executable = expected / "Contents/MacOS/Claude"
                self.assertEqual(executable.read_bytes(), launcher.read_bytes())
                self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
                self.assertEqual((expected / "Contents/MacOS/Claude.real").read_bytes(), source_executable)
                self.assertEqual(profiles.read_json(expected / "Contents/Resources/profile.json"), profile)
                icon = profiles.ET.fromstring((expected / "Contents/Resources/icon.svg").read_bytes())
                self.assertEqual(icon[0].get("fill"), profile["color"])
                self.assertEqual(icon[1].get("fill"), "#FCF2EE")
                self.assertEqual((expected / "Contents/Resources/profile.icns").read_bytes(), b"profile icon fixture")
                self.assertIn([launcher, "--make-flat-icons", staging / "icon.png", staging / "profile.iconset",
                               profile["color"], "W"], commands)
        self.assertEqual((self.source / "Contents/Info.plist").read_bytes(), source_plist)
        self.assertEqual((self.source / "Contents/MacOS/Claude").read_bytes(), source_executable)
        self.assertEqual(source_icon.read_bytes(), b"vendor icon")
        self.assertFalse((self.source / "Contents/MacOS/Claude.real").exists())
        self.assertFalse((resources / "profile.json").exists())

    def test_runtime_copy_refuses_changed_source_metadata_before_modifying_clone(self):
        self.set_source_metadata(CFBundleVersion="365562")
        original = self.create(mode="copy")
        source_plist = (self.source / "Contents/Info.plist").read_bytes()
        source_executable = (self.source / "Contents/MacOS/Claude").read_bytes()
        for change in ("copied_metadata", "sourceVersion", "sourceBuildVersion"):
            with self.subTest(change=change):
                staging = self.base / change
                staging.mkdir()
                expected = staging / (original["name"] + ".app")
                profile = original if change == "copied_metadata" else {**original, change: "stale"}

                def clone(command, **kwargs):
                    self.assertEqual(command, ["/bin/cp", "-cR", str(self.source.resolve()), str(expected)])
                    shutil.copytree(self.source, expected)
                    if change == "copied_metadata":
                        path = expected / "Contents/Info.plist"
                        info = {**plistlib.loads(path.read_bytes()), "VendorSetting": "changed during clone"}
                        path.write_bytes(plistlib.dumps(info))
                    return subprocess.CompletedProcess(command, 0)

                with mock.patch.object(profiles.subprocess, "run", side_effect=clone), \
                        mock.patch.object(profiles, "run", return_value="") as native, \
                        mock.patch.object(profiles, "rename_helpers") as helpers, \
                        mock.patch.object(profiles, "sign_copy") as sign:
                    with self.assertRaisesRegex(ValueError, "source app changed during the build"):
                        self.real_build_bundle(profile, self.base / "launcher", self.base / "icon.icns", staging)
                self.assertEqual(native.call_args_list, [
                    mock.call(["/usr/bin/codesign", "--verify", "--deep", "--strict", self.source.resolve()]),
                    mock.call(["/usr/bin/codesign", "--verify", "--deep", "--strict", expected]),
                ])
                helpers.assert_not_called()
                sign.assert_not_called()
                self.assertEqual((expected / "Contents/MacOS/Claude").read_bytes(), source_executable)
                self.assertFalse((expected / "Contents/MacOS/Claude.real").exists())
                self.assertFalse((expected / "Contents/Resources/profile.json").exists())
                self.assertEqual((self.source / "Contents/Info.plist").read_bytes(), source_plist)
                self.assertEqual((self.source / "Contents/MacOS/Claude").read_bytes(), source_executable)

    def test_cli_copy_modes_refresh_and_launcher_conversion(self):
        svg = self.svg_fixture()
        with mock.patch("builtins.print"), mock.patch.object(profiles, "running_profiles", side_effect=
                lambda entries: {entry["id"]: [] for entry in entries}):
            profiles.main(["create", "CLI copy", "--root", str(self.root), "--source", str(self.source),
                           "--apps-dir", str(self.apps), "--icon-svg", str(svg), "--mode", "copy"])
            created = profiles.registry(self.root)["profiles"][0]
            self.assertEqual(created["mode"], "copy")
            self.registration.assert_called_once_with(created)
            profiles.main(["set-icon", "CLI copy", "--root", str(self.root), "--mode", "launcher"])
            launcher = profiles.registry(self.root)["profiles"][0]
            self.assertEqual(launcher["mode"], "launcher")
            self.assertNotIn("runtimeExecutable", launcher)
            profiles.main(["set-icon", "CLI copy", "--root", str(self.root), "--mode", "copy", "--color", "purple"])
            converted = profiles.registry(self.root)["profiles"][0]
            self.set_source_metadata(CFBundleShortVersionString="5.0", CFBundleVersion="500")
            profiles.main(["refresh", "CLI copy", "--root", str(self.root)])
        refreshed = profiles.registry(self.root)["profiles"][0]
        self.assertEqual(refreshed["mode"], "copy")
        self.assertEqual(refreshed["sourceVersion"], "5.0")
        self.assertEqual(refreshed["sourceBuildVersion"], "500")
        self.assertEqual(refreshed["color"], profiles.COLORS["purple"])
        for key in ("id", "dataDirectory", "codeConfigDirectory", "appPath", "iconStyle"):
            self.assertEqual(refreshed[key], created[key], key)
        self.assertTrue(profiles.checked_bundle(refreshed).is_dir())
        self.assertEqual(self.registration.call_args_list,
                         [mock.call(created), mock.call(launcher), mock.call(converted), mock.call(refreshed)])

    def test_copy_policy_refuses_global_or_user_managed_preferences_including_symlinks(self):
        preferences = Path("/Library/Managed Preferences")
        filename = "com.anthropic.claudefordesktop.plist"
        paths = [preferences / filename, preferences / "fixture-user" / filename]
        for target, symlink in ((None, False), (paths[0], False), (paths[1], False),
                                (paths[0], True), (paths[1], True)):
            with self.subTest(target=target, symlink=symlink), \
                    mock.patch.object(profiles.pwd, "getpwuid", return_value=mock.Mock(pw_name="fixture-user")) as user, \
                    mock.patch.object(profiles.os, "getuid", return_value=501), \
                    mock.patch.object(Path, "exists", autospec=True, side_effect=
                                      lambda path: target is not None and path == target and not symlink) as exists, \
                    mock.patch.object(Path, "is_symlink", autospec=True, side_effect=
                                      lambda path: target is not None and path == target and symlink) as link:
                if target is None:
                    self.real_check_copy_policy()
                    self.assertEqual(exists.call_args_list, [mock.call(path) for path in paths])
                    self.assertEqual(link.call_args_list, [mock.call(path) for path in paths])
                else:
                    with self.assertRaisesRegex(ValueError, "device-managed.*administrator review"):
                        self.real_check_copy_policy()
                user.assert_called_once_with(501)

    def test_runtime_copy_checks_device_policy_before_native_commands_or_copying(self):
        profile = self.create(mode="copy")
        staging = self.base / "managed-copy"
        staging.mkdir()
        self.copy_policy.side_effect = ValueError("Claude is device-managed")
        with mock.patch.object(profiles, "run") as native, \
                mock.patch.object(profiles.subprocess, "run") as copy:
            with self.assertRaisesRegex(ValueError, "device-managed"):
                self.real_build_bundle(profile, self.base / "launcher", self.base / "icon.icns", staging)
        self.copy_policy.assert_called_once_with()
        native.assert_not_called()
        copy.assert_not_called()
        self.assertEqual(list(staging.iterdir()), [])
        self.assertTrue(profiles.checked_bundle(profile).is_dir())

    def test_register_bundle_is_best_effort_and_keeps_published_app_on_failure(self):
        profile = self.create(mode="copy")
        registry_before = (self.root / "_profiles.json").read_bytes()
        registrar = Path("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
        for error in (None, OSError("registrar unavailable"), ValueError("registration failed")):
            with self.subTest(error=error), mock.patch.object(profiles, "run", side_effect=error) as native, \
                    mock.patch("builtins.print") as warning:
                self.real_register_bundle(profile)
            native.assert_called_once_with([registrar, "-f", profile["appPath"]])
            if error is None:
                warning.assert_not_called()
            else:
                warning.assert_called_once()
                self.assertIn("Launch Services registration failed", warning.call_args.args[0])
                self.assertIs(warning.call_args.kwargs["file"], sys.stderr)
            self.assertTrue(profiles.checked_bundle(profile).is_dir())
            self.assertEqual((self.root / "_profiles.json").read_bytes(), registry_before)

    def test_cli_rejects_unknown_copy_mode(self):
        for action in ("create", "set-icon"):
            with self.subTest(action=action), mock.patch("sys.stderr"), mock.patch.object(profiles, "create_profile") as create, \
                    mock.patch.object(profiles, "update_icon") as update:
                with self.assertRaises(SystemExit) as error:
                    profiles.main([action, "CLI copy", "--root", str(self.root), "--mode", "unsupported"])
                self.assertEqual(error.exception.code, 2)
                create.assert_not_called()
                update.assert_not_called()

    def test_proxy_receipt_must_match_policy(self):
        receipt, policy = self.base / "receipt.json", self.base / "policy.json"
        profiles.write_json(receipt, {"command": "launcher-a"})
        profiles.write_json(policy, {"processWrapper": "launcher-b"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.real_proxy_available(receipt, policy)


if __name__ == "__main__":
    unittest.main()
