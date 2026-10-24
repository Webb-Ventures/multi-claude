import importlib.util
import json
from pathlib import Path
import plistlib
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
        mock.patch.object(profiles, "build_bundle", side_effect=self.fake_bundle).start()
        self.real_proxy_available = profiles.proxy_available
        self.proxy = mock.patch.object(profiles, "proxy_available", return_value=False).start()

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

    def test_proxy_receipt_must_match_policy(self):
        receipt, policy = self.base / "receipt.json", self.base / "policy.json"
        profiles.write_json(receipt, {"command": "launcher-a"})
        profiles.write_json(policy, {"processWrapper": "launcher-b"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.real_proxy_available(receipt, policy)


if __name__ == "__main__":
    unittest.main()
