import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "claude_code_proxy.py"
spec = importlib.util.spec_from_file_location("proxy_rc_under_test", SCRIPT)
proxy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proxy
spec.loader.exec_module(proxy)
TOKEN = "unit-test-credential-not-real"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RemoteControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "config.json"
        self.settings = {"gateway_urls": ["https://primary.example/"], "gateway_token": TOKEN}
        self.write_config()
        self.engine = self.root / "claude"
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        self.engine.chmod(0o700)
        self.gateway = proxy.Gateway("https://primary.example/", TOKEN)
        self.parent = {"ANTHROPIC_BASE_URL": "https://old.example", "ANTHROPIC_AUTH_TOKEN": "old",
                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "HTTPS_PROXY": "http://stale.example",
                       "CLAUDE_CONFIG_DIR": "/desktop/profile", "KEEP": "yes"}

    def write_config(self, **remote_control):
        settings = dict(self.settings)
        if remote_control:
            settings["remote_control"] = remote_control
        self.source.write_text(json.dumps(settings))
        return settings

    def test_remote_control_runs_without_gateway_and_says_so(self):
        for args in (["--rc"], ["remote-control"], ["--verbose", "--remote-control"]):
            with self.subTest(args=args), mock.patch.object(proxy, "select_gateway") as select:
                env, notice = proxy.launch_environment(args, self.settings, self.parent)
                select.assert_not_called()
            for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY",
                         "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
                self.assertNotIn(name, env)
            self.assertEqual(env["KEEP"], "yes")
            self.assertIn("directly to Anthropic", notice)
            self.assertNotIn(TOKEN, json.dumps(env) + notice)

    def test_ordinary_sessions_still_use_gateway(self):
        for args in (["-p", "rc"], ["--model", "rc", "prompt"], []):
            with self.subTest(args=args), mock.patch.object(proxy, "select_gateway", return_value=self.gateway):
                env, notice = proxy.launch_environment(args, self.settings, self.parent)
            self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"], notice),
                             (self.gateway.url, TOKEN, None))

    def test_forward_proxy_lane(self):
        ca = self.root / "ca.pem"
        ca.write_text("test certificate")
        with socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler) as listener:
            url = f"http://127.0.0.1:{listener.server_address[1]}"
            settings = self.write_config(forward_proxy_url=url, forward_proxy_ca=str(ca),
                                         config_dir=str(self.root / "rc-profile"))
            env, notice = proxy.launch_environment(["--rc"], settings, {"NO_PROXY": "internal.example"})
        self.assertEqual([env[name] for name in proxy.PROXY_ENV], [url] * 4)
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], str(ca))
        self.assertEqual(env["NO_PROXY"], "internal.example,localhost,127.0.0.1,::1,primary.example")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.root / "rc-profile"))
        self.assertIn("forward proxy", notice)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_unreachable_forward_proxy_fails_closed(self):
        settings = self.write_config(forward_proxy_url=f"http://127.0.0.1:{free_port()}")
        with self.assertRaisesRegex(ValueError, "not falling back"):
            proxy.launch_environment(["--rc"], settings, {})

    def test_invalid_remote_control_settings(self):
        for rc, message in (({"enabled": False}, "turned off"), ({"surprise": 1}, "Unknown"),
                            ({"enabled": "yes"}, "true or false"),
                            ({"forward_proxy_ca": "/ca.pem"}, "needs"),
                            ({"forward_proxy_url": "http://public.example:9801"}, "forward_proxy_url"),
                            ({"config_dir": "relative"}, "absolute"),
                            ({"forward_proxy_url": "http://127.0.0.1:9", "forward_proxy_ca": "/missing/ca.pem"}, "readable")):
            with self.subTest(rc=rc), self.assertRaisesRegex(ValueError, message):
                proxy.launch_environment(["--rc"], {**self.settings, "remote_control": rc}, {})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            proxy.launch_environment(["--rc"], {**self.settings, "remote_control": []}, {})

    def test_desktop_exec_keeps_its_config_dir_for_remote_control(self):
        self.write_config(config_dir=str(self.root / "rc-profile"))
        stderr = io.StringIO()
        with mock.patch.dict(proxy.os.environ, self.parent, clear=True), \
                mock.patch.object(proxy.os, "execve") as execute, contextlib.redirect_stderr(stderr):
            proxy.execute_engine([str(self.engine), "--remote-control"], self.source)
        env = execute.call_args.args[2]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/desktop/profile")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertIn("Remote Control", stderr.getvalue())

    def test_run_finds_claude_and_routes_by_session_type(self):
        self.write_config(config_dir=str(self.root / "rc-profile"))
        with mock.patch.object(proxy.shutil, "which", return_value=str(self.engine)), \
                mock.patch.object(proxy, "select_gateway", return_value=self.gateway), \
                mock.patch.dict(proxy.os.environ, {"PATH": "/usr/bin"}, clear=True), \
                mock.patch.object(proxy.os, "execve") as execute, \
                mock.patch.object(proxy.os, "chdir") as chdir, contextlib.redirect_stderr(io.StringIO()):
            proxy.run_cli(["--", "--model", "test-model"], self.source)
            self.assertEqual(execute.call_args.args[:2], (str(self.engine), [str(self.engine), "--model", "test-model"]))
            self.assertEqual(execute.call_args.args[2]["ANTHROPIC_AUTH_TOKEN"], TOKEN)
            proxy.run_cli(["remote-control"], self.source, directory=self.root)
            env = execute.call_args.args[2]
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.root / "rc-profile"))
            chdir.assert_called_once_with(self.root)

    def test_run_requires_an_executable_claude(self):
        with mock.patch.object(proxy.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "--claude"):
            proxy.run_cli([], self.source)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            proxy.run_cli([], self.source, claude=str(self.root / "missing"))
        with self.assertRaisesRegex(ValueError, "directory"):
            proxy.run_cli([], self.source, claude=str(self.engine), directory=self.root / "missing")


class LinuxServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "config.json"
        self.source.write_text(json.dumps({"gateway_urls": ["https://primary.example/"], "gateway_token": TOKEN}))
        self.engine = self.root / "claude"
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        self.engine.chmod(0o700)
        self.units = self.root / "systemd/user"
        self.state = self.root / "state/linux-service.json"
        self.unit = self.units / proxy.SERVICE_NAME

    def install(self, args=("--", "remote-control", "--name", "100% $HOME \"x\"")):
        with mock.patch.object(proxy, "systemctl") as systemctl, mock.patch.object(proxy.os, "geteuid", return_value=1000), \
                mock.patch.object(proxy.pwd, "getpwuid", return_value=mock.Mock(pw_name="tester")), \
                contextlib.redirect_stdout(io.StringIO()):
            proxy.install_service(self.source, self.root, list(args), str(self.engine), self.units, self.state)
        return systemctl

    def test_service_unit_is_quoted_and_holds_no_secret(self):
        systemctl = self.install()
        text = self.unit.read_text()
        self.assertTrue(text.startswith(proxy.SERVICE_MARKER + "\n"))
        self.assertNotIn(TOKEN, text)
        exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertIn('"run" "--config" "%s" "--claude" "%s" "--" "remote-control" "--name"' % (self.source.resolve(), self.engine), exec_start)
        self.assertIn('"100%% $$HOME \\"x\\""', exec_start)
        self.assertIn("Restart=on-failure", text)
        self.assertEqual([c.args for c in systemctl.call_args_list],
                         [("daemon-reload",), ("enable", "--now", proxy.SERVICE_NAME)])
        self.assertEqual(json.loads(self.state.read_text())["unit"], str(self.unit))

    def test_default_service_session_is_remote_control(self):
        self.install(args=())
        self.assertIn('"--" "remote-control"', self.unit.read_text())

    def test_service_refuses_non_remote_control_and_foreign_units(self):
        with self.assertRaisesRegex(ValueError, "Remote Control session"):
            self.install(args=("--", "-p", "prompt"))
        self.units.mkdir(parents=True)
        self.unit.write_text("[Service]\nExecStart=/bin/true\n")
        for action in (self.install, lambda: proxy.uninstall_service(self.units, self.state),
                       lambda: proxy.service_status(self.units)):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "not created by this tool"):
                action()
        self.assertEqual(self.unit.read_text(), "[Service]\nExecStart=/bin/true\n")

    def test_uninstall_and_status(self):
        self.install()
        result = mock.Mock(stdout="active\n")
        with mock.patch.object(proxy, "systemctl", return_value=result), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(proxy.service_status(self.units), "active")
        self.assertIn("active", out.getvalue())
        with mock.patch.object(proxy, "systemctl") as systemctl, contextlib.redirect_stdout(io.StringIO()):
            proxy.uninstall_service(self.units, self.state)
            proxy.uninstall_service(self.units, self.state)  # Idempotent.
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(systemctl.call_args_list[0].args, ("disable", "--now", proxy.SERVICE_NAME))
        with self.assertRaisesRegex(ValueError, "not installed"):
            proxy.service_status(self.units)

    def test_root_is_refused(self):
        with mock.patch.object(proxy.os, "geteuid", return_value=0), self.assertRaisesRegex(ValueError, "not root"):
            proxy.install_service(self.source, self.root, [], str(self.engine), self.units, self.state)

    def test_missing_systemd_points_to_run(self):
        with mock.patch.object(proxy.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "tmux"):
            proxy.systemctl("daemon-reload")

    def test_linux_check_skips_desktop_and_reports_remote_control(self):
        gateway = proxy.Gateway("https://primary.example/", TOKEN)
        with mock.patch.object(proxy, "IS_MACOS", False), mock.patch.object(proxy, "check_app") as check_app, \
                mock.patch.object(proxy, "select_gateway", return_value=gateway), \
                mock.patch.object(proxy, "gateway_models", return_value=["model"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            proxy.main(["check", "--config", str(self.source)])
        check_app.assert_not_called()
        self.assertIn("Not macOS", out.getvalue())
        self.assertIn("Remote Control: allowed", out.getvalue())

    def test_linux_install_dispatches_to_service(self):
        with mock.patch.object(proxy, "IS_MACOS", False), mock.patch.object(proxy, "SERVICE_STATE", self.state), \
                mock.patch.object(proxy, "install_service") as install:
            proxy.main(["install", "--config", str(self.source), "--dir", str(self.root), "--", "remote-control"])
        install.assert_called_once_with(self.source, self.root, ["--", "remote-control"], None)
        with mock.patch.object(proxy, "IS_MACOS", True), self.assertRaisesRegex(ValueError, "Linux"):
            proxy.main(["install", "--config", str(self.source), "--", "remote-control"])


if __name__ == "__main__":
    unittest.main()
