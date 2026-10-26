import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "claude_code_proxy.py"
spec = importlib.util.spec_from_file_location("desktop_proxy_under_test", SCRIPT)
proxy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proxy
spec.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "config.json"
        self.write_json(self.source, {"gateway_urls": ["https://primary.example/", "https://fallback.example/"],
                                      "gateway_token": "test-token"})
        self.gateway = proxy.Gateway("https://primary.example/", "test-token")
        self.policy = self.root / "managed-settings.json"
        self.policy_state = self.root / "policy-state.json"
        self.library = self.root / "library"
        self.state = self.root / "state.json"

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def make_library(self, config=None):
        old_id = str(uuid.uuid4())
        self.write_json(self.library / "_meta.json", {
            "appliedId": old_id, "entries": [{"id": old_id, "name": "Original"}],
        })
        self.write_json(self.library / (old_id + ".json"), config or {})
        return old_id

    def make_installation(self):
        old_id = self.make_library({"inferenceProvider": "anthropic"})
        previous, own_id, meta = proxy.local_plan(self.library)
        self.write_json(self.library / "_meta.json", meta)
        self.write_json(self.library / (own_id + ".json"), {proxy.ENABLE_KEY: True})
        self.write_json(self.state, {"previous": previous, "profile": own_id,
                                    "source": str(self.source), "command": "test-wrapper"})
        return old_id, own_id

    def test_configured_primary_and_fallback(self):
        gateways = proxy.load_gateways(self.source)
        self.assertEqual([g.url for g in gateways], ["https://primary.example/", "https://fallback.example/"])
        self.assertEqual(gateways[0].token, "test-token")
        self.assertNotIn("test-token", repr(gateways[0]))

    def test_single_local_gateway_configuration(self):
        self.write_json(self.source, {"gateway_urls": ["http://127.0.0.1:8317"], "gateway_token": "test-token"})
        self.assertEqual(proxy.load_gateways(self.source)[0].url, "http://127.0.0.1:8317/")

    def test_shell_expressions_are_not_executed(self):
        marker = self.root / "executed"
        self.write_json(self.source, {"gateway_urls": [f"$(touch {marker})"], "gateway_token": "test-token"})
        with self.assertRaises(ValueError):
            proxy.load_gateways(self.source)
        self.assertFalse(marker.exists())

    def test_fallback_selection_and_fail_closed(self):
        gateways = proxy.load_gateways(self.source)
        connection = mock.MagicMock()
        with mock.patch.object(proxy.socket, "create_connection", side_effect=[OSError(), connection]):
            self.assertEqual(proxy.select_gateway(gateways), gateways[1])
        with mock.patch.object(proxy.socket, "create_connection", side_effect=OSError()):
            with self.assertRaisesRegex(ValueError, "No direct fallback"):
                proxy.select_gateway(gateways)

    def test_engine_environment_does_not_modify_parent_or_use_account_token(self):
        parent = {"ANTHROPIC_API_KEY": "account-secret", "CLAUDE_CODE_OAUTH_TOKEN": "account-oauth",
                  "ANTHROPIC_BASE_URL": "https://old.example", "CLAUDE_CODE_USE_BEDROCK": "1",
                  "NO_PROXY": "localhost", "no_proxy": "other.example", "SESSION_MARKER": "keep"}
        original = parent.copy()
        env = proxy.engine_environment(parent, self.gateway)
        self.assertEqual(parent, original)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "test-token")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], self.gateway.url)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertEqual(env["SESSION_MARKER"], "keep")
        self.assertEqual(env["NO_PROXY"], "localhost,other.example,primary.example")
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])

    def test_exact_engine_and_arguments_are_execed(self):
        engine = self.root / "bundled-claude"
        engine.write_text("#!/bin/sh\nexit 0\n")
        engine.chmod(0o700)
        args = [str(engine), "--version", "argument with spaces"]
        with mock.patch.object(proxy, "select_gateway", return_value=self.gateway), \
                mock.patch.object(proxy.os, "execve") as execute:
            proxy.execute_engine(["--", *args], self.source)
        self.assertEqual(execute.call_args.args[:2], (str(engine), args))
        self.assertEqual(execute.call_args.args[2]["ANTHROPIC_AUTH_TOKEN"], "test-token")

    def test_relative_engine_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            proxy.execute_engine(["claude", "--version"], self.source)

    def test_wrapper_command_contains_no_secret(self):
        command = proxy.wrapper_command(self.source)
        parts = json.loads(command)
        self.assertTrue(Path(parts[0]).is_absolute())
        self.assertEqual(parts[-1], "--")
        self.assertNotIn("test-token", command)

    def test_process_handshake_stdout_is_unmodified(self):
        handler = socketserver.BaseRequestHandler
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as listener:
            port = listener.server_address[1]
            self.write_json(self.source, {"gateway_urls": [f"http://127.0.0.1:{port}/"], "gateway_token": "test-token"})
            engine = self.root / "bundled-claude"
            engine.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\"\n")
            engine.chmod(0o700)
            command = json.loads(proxy.wrapper_command(self.source)) + [str(engine), "--version"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "--version\n")
        self.assertEqual(result.stderr, "")

    def test_model_redirect_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no credential"):
            proxy.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_policy_install_merge_idempotent_and_uninstall(self):
        self.write_json(self.policy, {"permissions": {"deny": ["Read(secret)"]}})
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        settings = json.loads(self.policy.read_text())
        settings["new-setting"] = True
        self.write_json(self.policy, settings)
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertEqual(json.loads(self.policy.read_text()), {
            "permissions": {"deny": ["Read(secret)"]}, "new-setting": True,
        })
        self.assertFalse(self.policy_state.exists())

    def test_new_empty_policy_is_removed_on_uninstall(self):
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertFalse(self.policy.exists())

    def test_existing_managed_wrapper_is_not_overwritten(self):
        for settings in ({"processWrapper": "other"}, {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": "other"}},
                         {"forceLoginMethod": "claudeai"}):
            self.write_json(self.policy, settings)
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
            self.assertEqual(json.loads(self.policy.read_text()), settings)
            self.assertFalse(self.policy_state.exists())

    def test_changed_managed_wrapper_is_not_overwritten_on_undo(self):
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.write_json(self.policy, {"processWrapper": "changed"})
        with self.assertRaisesRegex(ValueError, "changed"):
            proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertEqual(json.loads(self.policy.read_text())["processWrapper"], "changed")

    def test_policy_install_failure_cleans_receipt(self):
        original = proxy.write_object
        def fail_policy(path, value, mode=0o600):
            if path == self.policy:
                raise OSError("test write failure")
            return original(path, value, mode)
        with mock.patch.object(proxy, "write_object", side_effect=fail_policy):
            with self.assertRaises(OSError):
                proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.assertFalse(self.policy_state.exists())

    def test_interrupted_policy_install_can_be_resumed(self):
        original = proxy.write_object
        def interrupt_policy(path, value, mode=0o600):
            if path == self.policy:
                raise KeyboardInterrupt()
            return original(path, value, mode)
        with mock.patch.object(proxy, "write_object", side_effect=interrupt_policy):
            with self.assertRaises(KeyboardInterrupt):
                proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.assertTrue(self.policy_state.exists())
        self.assertFalse(self.policy.exists())
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.assertEqual(json.loads(self.policy.read_text())["processWrapper"], "test-wrapper")

    def test_pending_policy_install_can_be_cancelled(self):
        self.write_json(self.policy_state, {"command": "test-wrapper", "uid": 501,
                                           "existed": False, "mode": 0o644})
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertFalse(self.policy_state.exists())
        self.assertFalse(self.policy.exists())

    def test_policy_uninstall_is_idempotent(self):
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertFalse(self.policy.exists())

    def test_interrupted_policy_removal_can_finish(self):
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.policy.unlink()
        proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertFalse(self.policy_state.exists())

    def test_unrecorded_foreign_policy_is_not_removed(self):
        self.write_json(self.policy, {"processWrapper": "foreign"})
        with self.assertRaisesRegex(ValueError, "unrecorded"):
            proxy.uninstall_policy(501, self.policy, self.policy_state)
        self.assertTrue(self.policy.exists())

    def test_other_user_cannot_replace_or_remove_installation(self):
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        with self.assertRaises(ValueError):
            proxy.install_policy("test-wrapper", 502, self.policy, self.policy_state)
        with self.assertRaises(ValueError):
            proxy.uninstall_policy(502, self.policy, self.policy_state)
        self.assertTrue(self.policy_state.exists())

    def test_transaction_lock_refuses_concurrent_changes(self):
        with proxy.transaction_lock(self.state):
            with self.assertRaisesRegex(ValueError, "in progress"):
                with proxy.transaction_lock(self.state):
                    self.fail("Second lock should not be acquired")
        with proxy.transaction_lock(self.state):
            pass

    def test_symlink_policy_is_rejected(self):
        target = self.root / "target.json"
        self.write_json(target, {})
        self.policy.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        self.assertEqual(target.read_text(), "{}")

    def test_configuration_plan_is_first_party_and_preserves_original(self):
        old_id = self.make_library({"inferenceProvider": "anthropic", "inferenceCredentialKind": "apiKey"})
        previous, profile_id, meta = proxy.local_plan(self.library)
        self.assertEqual(previous, old_id)
        self.assertNotEqual(profile_id, old_id)
        self.assertEqual(meta["appliedId"], profile_id)
        self.assertEqual(len(meta["entries"]), 2)
        self.assertEqual(json.loads((self.library / "_meta.json").read_text())["appliedId"], old_id)

    def test_additional_settings_are_not_discarded(self):
        self.make_library({"disableAutoUpdates": True})
        with self.assertRaisesRegex(ValueError, "additional settings"):
            proxy.local_plan(self.library)

    def test_hybrid_deployment_is_not_overridden(self):
        self.write_json(self.library / "_meta.json", {"hybridPointer": "organization-bootstrap"})
        with self.assertRaisesRegex(ValueError, "not be overridden"):
            proxy.local_plan(self.library)

    def test_invalid_configuration_id_is_rejected(self):
        self.write_json(self.library / "_meta.json", {"appliedId": "../../outside"})
        with self.assertRaisesRegex(ValueError, "Invalid"):
            proxy.local_plan(self.library)

    def test_uninstall_restores_previous_and_preserves_new_entries(self):
        old_id, own_id = self.make_installation()
        meta = json.loads((self.library / "_meta.json").read_text())
        meta["entries"].append({"id": "new-config", "name": "Added later"})
        self.write_json(self.library / "_meta.json", meta)
        with mock.patch.object(proxy, "require_quit"), mock.patch.object(proxy, "privileged"), contextlib.redirect_stdout(io.StringIO()):
            proxy.uninstall(self.source, self.library, self.state)
        updated = json.loads((self.library / "_meta.json").read_text())
        self.assertEqual(updated["appliedId"], old_id)
        self.assertEqual([entry["id"] for entry in updated["entries"]], [old_id, "new-config"])
        self.assertFalse((self.library / (own_id + ".json")).exists())

    def test_uninstall_keeps_user_selected_other_profile(self):
        old_id, own_id = self.make_installation()
        meta = json.loads((self.library / "_meta.json").read_text())
        meta["appliedId"] = old_id
        self.write_json(self.library / "_meta.json", meta)
        with mock.patch.object(proxy, "require_quit"), mock.patch.object(proxy, "privileged"), contextlib.redirect_stdout(io.StringIO()):
            proxy.uninstall(self.source, self.library, self.state)
        self.assertEqual(json.loads((self.library / "_meta.json").read_text())["appliedId"], old_id)

    def test_edited_profile_is_not_deleted(self):
        _, own_id = self.make_installation()
        self.write_json(self.library / (own_id + ".json"), {proxy.ENABLE_KEY: False})
        with mock.patch.object(proxy, "require_quit"), mock.patch.object(proxy, "privileged") as privileged:
            with self.assertRaisesRegex(ValueError, "changed"):
                proxy.uninstall(self.source, self.library, self.state)
        privileged.assert_not_called()

    def test_failed_local_uninstall_can_be_retried_after_policy_removal(self):
        old_id, own_id = self.make_installation()
        proxy.install_policy("test-wrapper", 501, self.policy, self.policy_state)
        original = proxy.write_object
        def fail_meta(path, value, mode=0o600):
            if path == self.library / "_meta.json":
                raise OSError("test metadata failure")
            return original(path, value, mode)
        def remove_policy(action, source):
            self.assertEqual(action, "_uninstall-policy")
            proxy.uninstall_policy(501, self.policy, self.policy_state)
        with mock.patch.object(proxy, "require_quit"), \
                mock.patch.object(proxy, "privileged", side_effect=remove_policy), \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(proxy, "write_object", side_effect=fail_meta):
                with self.assertRaises(OSError):
                    proxy.uninstall(self.source, self.library, self.state)
            self.assertFalse(self.policy_state.exists())
            self.assertTrue(self.state.exists())
            proxy.uninstall(self.source, self.library, self.state)
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads((self.library / "_meta.json").read_text())["appliedId"], old_id)

    def test_interrupted_local_install_is_recoverable_at_each_write(self):
        for existing in (False, True):
            for point in ("before-policy", "after-policy", "before-profile", "before-meta", "after-meta"):
                with self.subTest(existing=existing, point=point):
                    case = self.root / (str(existing) + "-" + point)
                    self.library, self.state = case / "library", case / "state.json"
                    self.policy, self.policy_state = case / "policy.json", case / "policy-state.json"
                    original_meta = None
                    if existing:
                        self.make_library()
                        original_meta = json.loads((self.library / "_meta.json").read_text())
                    self.write_json(self.policy, {"unrelated": True})
                    real_write = proxy.write_object
                    def interrupt_write(path, value, mode=0o600):
                        if ((point == "before-profile" and path.parent == self.library and path.name != "_meta.json")
                                or (point == "before-meta" and path == self.library / "_meta.json")
                                or (point == "after-meta" and path == self.state and value.get("phase") == "installed")):
                            raise KeyboardInterrupt()
                        return real_write(path, value, mode)
                    def policy_step(action, source):
                        if action == "_install-policy":
                            if point == "before-policy":
                                raise KeyboardInterrupt()
                            proxy.install_policy(proxy.wrapper_command(source), 501, self.policy, self.policy_state)
                            if point == "after-policy":
                                raise KeyboardInterrupt()
                        else:
                            proxy.uninstall_policy(501, self.policy, self.policy_state)
                    with mock.patch.object(proxy, "check_app"), mock.patch.object(proxy, "require_quit"), \
                            mock.patch.object(proxy, "check_desktop_policy"), \
                            mock.patch.object(proxy, "select_gateway", return_value=self.gateway), \
                            mock.patch.object(proxy, "gateway_models", return_value=["model"]), \
                            mock.patch.object(proxy, "privileged", side_effect=policy_step), \
                            contextlib.redirect_stdout(io.StringIO()):
                        with mock.patch.object(proxy, "write_object", side_effect=interrupt_write):
                            with self.assertRaises(KeyboardInterrupt):
                                proxy.install(self.source, self.root / "Claude.app", self.library, self.state)
                        self.assertTrue(self.state.exists())
                        with self.assertRaisesRegex(ValueError, "interrupted"):
                            proxy.installation_status(self.state, self.library, self.policy)
                        proxy.uninstall(self.source, self.library, self.state)
                    self.assertFalse(self.state.exists())
                    self.assertFalse(self.policy_state.exists())
                    self.assertEqual(json.loads(self.policy.read_text()), {"unrelated": True})
                    if existing:
                        self.assertEqual(json.loads((self.library / "_meta.json").read_text()), original_meta)
                    elif point != "after-meta":
                        self.assertFalse((self.library / "_meta.json").exists())

    def test_interrupted_uninstall_after_profile_deletion_can_finish(self):
        old_id, own_id = self.make_installation()
        real_unlink = Path.unlink
        def interrupt_unlink(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            if path == self.library / (own_id + ".json"):
                raise KeyboardInterrupt()
        with mock.patch.object(proxy, "require_quit"), mock.patch.object(proxy, "privileged"), \
                contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(Path, "unlink", autospec=True, side_effect=interrupt_unlink):
                with self.assertRaises(KeyboardInterrupt):
                    proxy.uninstall(self.source, self.library, self.state)
            self.assertTrue(self.state.exists())
            self.assertFalse((self.library / (own_id + ".json")).exists())
            proxy.uninstall(self.source, self.library, self.state)
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads((self.library / "_meta.json").read_text())["appliedId"], old_id)

    def test_failed_rollback_keeps_recovery_receipt(self):
        real_write = proxy.write_object
        def fail_profile(path, value, mode=0o600):
            if path.parent == self.library:
                raise OSError("profile write failed")
            return real_write(path, value, mode)
        def policy_step(action, source):
            if action == "_install-policy":
                proxy.install_policy(proxy.wrapper_command(source), 501, self.policy, self.policy_state)
            else:
                raise ValueError("simulated sudo failure")
        with mock.patch.object(proxy, "check_app"), mock.patch.object(proxy, "require_quit"), \
                mock.patch.object(proxy, "check_desktop_policy"), \
                mock.patch.object(proxy, "select_gateway", return_value=self.gateway), \
                mock.patch.object(proxy, "gateway_models", return_value=["model"]), \
                mock.patch.object(proxy, "privileged", side_effect=policy_step), \
                mock.patch.object(proxy, "write_object", side_effect=fail_profile), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "sudo failure"):
                proxy.install(self.source, self.root / "Claude.app", self.library, self.state)
        self.assertTrue(self.state.exists())
        self.assertTrue(self.policy_state.exists())
        self.assertEqual(json.loads(self.state.read_text())["phase"], "removing")
        with mock.patch.object(proxy, "require_quit"), \
                mock.patch.object(proxy, "privileged", side_effect=lambda *_: proxy.uninstall_policy(501, self.policy, self.policy_state)), \
                contextlib.redirect_stdout(io.StringIO()):
            proxy.uninstall(self.source, self.library, self.state)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.policy.exists())

    def test_partial_local_install_rolls_back_policy_and_keeps_original_meta(self):
        old_id = self.make_library()
        original = proxy.write_object
        def fail_meta(path, value, mode=0o600):
            if path == self.library / "_meta.json":
                raise OSError("test metadata failure")
            return original(path, value, mode)
        with mock.patch.object(proxy, "check_app"), mock.patch.object(proxy, "require_quit"), \
                mock.patch.object(proxy, "check_desktop_policy"), \
                mock.patch.object(proxy, "select_gateway", return_value=self.gateway), \
                mock.patch.object(proxy, "gateway_models", return_value=["model"]), \
                mock.patch.object(proxy, "privileged") as privileged, \
                mock.patch.object(proxy, "write_object", side_effect=fail_meta), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                proxy.install(self.source, self.root / "Claude.app", self.library, self.state)
        self.assertEqual([call.args[0] for call in privileged.call_args_list], ["_install-policy", "_uninstall-policy"])
        self.assertEqual(json.loads((self.library / "_meta.json").read_text())["appliedId"], old_id)
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.library.glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
