import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxy_claude_rc_under_test", ROOT / "claude_code_proxy.py")
proxy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proxy
spec.loader.exec_module(proxy)
TOKEN = "unit-test-credential-not-real"


class ClaudeRcTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / "home"
        self.home.mkdir()
        self.account("default", "default@example.com")
        self.work = self.account("work", "work@example.com")
        self.unsigned = self.account("unsigned", None, signed_in=False)
        (self.home / ".claude-not a profile").mkdir()
        (self.home / ".claude-file").write_text("not a directory")
        self.engine = self.home / "claude"
        self.engine.write_text("#!/bin/sh\nexit 0\n")
        self.engine.chmod(0o700)
        self.source = self.home / "config.json"
        for patcher in (mock.patch.object(proxy.Path, "home", return_value=self.home),
                        mock.patch.object(proxy, "IS_MACOS", False),
                        mock.patch.object(proxy, "running_remote_control", return_value=[]),
                        mock.patch.dict(proxy.os.environ, {"PATH": "/usr/bin", "HOME": str(self.home),
                                                           "ANTHROPIC_BASE_URL": "https://gw.example",
                                                           "ANTHROPIC_AUTH_TOKEN": TOKEN,
                                                           "CLAUDE_CONFIG_DIR": str(self.work)}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def account(self, name, email, signed_in=True):
        directory = self.home / (".claude" if name == "default" else ".claude-" + name)
        directory.mkdir()
        if signed_in:
            (directory / ".credentials.json").write_text("{}")
        state = self.home / ".claude.json" if name == "default" else directory / ".claude.json"
        state.write_text(json.dumps({"oauthAccount": {"emailAddress": email}} if email else {}))
        return directory

    def rc(self, *args, source=None):
        err = io.StringIO()
        with mock.patch.object(proxy.os, "execve") as execute, contextlib.redirect_stderr(err):
            proxy.rc_command(list(args), source or self.source, claude=str(self.engine))
        return execute.call_args.args, err.getvalue()

    def test_profiles_come_from_claude_dirs_and_config(self):
        self.assertEqual(proxy.profile_dirs({}), {"default": None, "unsigned": self.unsigned, "work": self.work})
        extra = self.home / "elsewhere"
        profiles = proxy.profile_dirs({"profiles": {"client": str(extra), "work": "~/alt"}})
        self.assertEqual((profiles["client"], profiles["work"]), (extra, self.home / "alt"))
        for bad in ({"profiles": []}, {"profiles": {"default": "~/x"}}, {"profiles": {"a b": "~/x"}},
                    {"profiles": {"x": "relative"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                proxy.profile_dirs(bad)

    def test_named_profile_runs_remote_control_on_that_account(self):
        (path, argv, env), notice = self.rc("work", "--spawn", "worktree")
        self.assertEqual((path, argv), (str(self.engine), [str(self.engine), "remote-control", "--spawn", "worktree"]))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.work))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertIn("work (work@example.com)", notice)
        self.assertIn("directly to Anthropic", notice)

    def test_default_profile_ignores_an_exported_config_dir(self):
        for args in ((), ("default",), ("--name", "srv"), ("--", "--name", "srv")):
            with self.subTest(args=args):
                (_, argv, env), notice = self.rc(*args)
                self.assertNotIn("CLAUDE_CONFIG_DIR", env)
                self.assertEqual(argv[1], "remote-control")
                self.assertIn("default (default@example.com)", notice)
        self.assertEqual(self.rc("--name", "srv")[0][1][2:], ["--name", "srv"])

    def test_unknown_and_unsigned_profiles_explain_what_to_do(self):
        with self.assertRaisesRegex(ValueError, r"Unknown profile 'nope'\. Available: default, unsigned, work"):
            self.rc("nope")
        with self.assertRaisesRegex(ValueError, f"CLAUDE_CONFIG_DIR={self.unsigned} claude, then /login"):
            self.rc("unsigned")
        with mock.patch.object(proxy, "IS_MACOS", True):  # Keychain sign-in cannot be checked.
            self.assertEqual(self.rc("unsigned")[0][2]["CLAUDE_CONFIG_DIR"], str(self.unsigned))

    def test_gateway_config_is_optional_but_forward_proxy_is_honoured(self):
        self.assertFalse(self.source.exists())
        self.rc("work")
        self.source.write_text(json.dumps({"gateway_urls": ["https://gw.example/"], "gateway_token": TOKEN,
                                           "remote_control": {"forward_proxy_url": "http://127.0.0.1:9"}}))
        with self.assertRaisesRegex(ValueError, "not falling back"):
            self.rc("work")
        self.source.write_text(json.dumps({"gateway_urls": ["https://gw.example/"], "gateway_token": TOKEN,
                                           "remote_control": {"enabled": False}}))
        with self.assertRaisesRegex(ValueError, "turned off"):
            self.rc("work")

    def test_list_shows_accounts_and_sign_in(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            proxy.rc_command(["--list"], self.source)
        lines = out.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["default", "unsigned", "work"])
        self.assertIn("default@example.com", lines[0])
        self.assertIn("NOT signed in", lines[1])
        self.assertTrue(lines[2].endswith("signed in") and "work@example.com" in lines[2])

    def test_duplicate_session_is_noted_not_blocked(self):
        with mock.patch.object(proxy, "running_remote_control", return_value=[4242]):
            _, notice = self.rc("work")
        self.assertIn("already running here (pid 4242)", notice)

    def test_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            proxy.rc_command(["--help"], self.source)
        self.assertIn("usage: claude-rc [PROFILE]", out.getvalue())


class ClaudeRcScriptTests(unittest.TestCase):
    def test_symlinked_script_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude-work").mkdir()
            (home / ".claude-work/.credentials.json").write_text("{}")
            bin_dir = home / "bin"
            bin_dir.mkdir()
            (bin_dir / "claude-rc").symlink_to(ROOT / "claude-rc")
            fake = bin_dir / "claude"
            fake.write_text('#!/bin/sh\necho "ARGS=$*"\necho "CFG=${CLAUDE_CONFIG_DIR:-unset}"\n'
                            'echo "BASE=${ANTHROPIC_BASE_URL:-unset}"\n')
            fake.chmod(0o700)
            env = {"HOME": str(home), "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
                   "ANTHROPIC_BASE_URL": "https://gw.example", "MULTI_CLAUDE_CONFIG": str(home / "none.json")}
            result = subprocess.run(["claude-rc", "work", "--name", "srv"], capture_output=True, text=True,
                                    env=env, cwd=home, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["ARGS=remote-control --name srv", f"CFG={home / '.claude-work'}", "BASE=unset"])
        self.assertIn("claude-rc: work", result.stderr)

    @unittest.skipUnless(Path("/proc/self/cmdline").exists(), "needs /proc")
    def test_running_remote_control_scan(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            # A process whose argv is exactly ["…/claude", "rc"], like the real native binary:
            # Python started through a symlink named claude, running a script file named rc.
            fake = Path(directory) / "claude"
            fake.symlink_to(sys.executable)
            (Path(directory) / "rc").write_text("import time\ntime.sleep(30)\n")
            account = Path(directory) / ".claude-work"
            process = subprocess.Popen([str(fake), "rc"], cwd=directory,
                                       env={"CLAUDE_CONFIG_DIR": str(account), "PATH": "/usr/bin:/bin"})
            try:
                for _ in range(50):
                    if proxy.running_remote_control(account, Path(directory).resolve()):
                        break
                    time.sleep(0.1)
                self.assertIn(process.pid, proxy.running_remote_control(account, Path(directory).resolve()))
                self.assertNotIn(process.pid, proxy.running_remote_control(None, Path(directory).resolve()))
            finally:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
