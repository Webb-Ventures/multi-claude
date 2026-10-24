import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_cli", ROOT / "claude_cli.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
TOKEN = "unit-test-credential-not-real"
SETTINGS = {"gateway_urls": ["http://localhost:8317/"], "gateway_token": TOKEN}


class CliTests(unittest.TestCase):
    def test_folder_and_argv_handling(self):
        with tempfile.TemporaryDirectory() as folder:
            for prefix in (["-C", folder], ["--dir", folder], ["--dir=" + folder], [folder]):
                args = ["--model", "test-model", "prompt with spaces", "--config", "untouched"]
                self.assertEqual(cli.parse_args(["--config", "chosen.json", *prefix, *args]), ("chosen.json", folder, args))
            for args in (["--", folder, "--dir", "untouched"], ["a prompt", "-C", folder]):
                self.assertEqual(cli.parse_args(args), (None, None, args))
        with patch.object(Path, "is_dir", return_value=True):
            for command in cli.SUBCOMMANDS:
                self.assertEqual(cli.parse_args([command, "list"]), (None, None, [command, "list"]))
        for args in (["-C"], ["--config"], ["--dir="], ["--dir", "/nonexistent-test-directory"]):
            with self.assertRaises(ValueError):
                cli.parse_args(args)

    def test_environment_and_config_dir(self):
        inherited = {"ANTHROPIC_API_KEY": "wrong", "CLAUDE_CODE_OAUTH_TOKEN": "wrong",
                     "ANTHROPIC_CUSTOM_HEADERS": "wrong", "CLAUDE_CODE_USE_BEDROCK": "1",
                     "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
                     "CLAUDE_CONFIG_DIR": "/keep-profile", "API_TIMEOUT_MS": "900000"}
        gateway = cli.config.Gateway(SETTINGS["gateway_urls"][0], TOKEN)
        with patch.dict(os.environ, inherited, clear=True):
            env = cli.gateway_environment(gateway, SETTINGS)
            self.assertTrue(all(key not in env for key in inherited if key not in ("CLAUDE_CONFIG_DIR", "API_TIMEOUT_MS")))
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/keep-profile")
            self.assertEqual(env["API_TIMEOUT_MS"], "900000")
            self.assertEqual((env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_AUTH_TOKEN"]), (gateway.url, TOKEN))
            self.assertEqual(cli.gateway_environment(gateway, {"cli_config_dir": "/explicit-profile"})["CLAUDE_CONFIG_DIR"], "/explicit-profile")
            self.assertEqual(dict(os.environ), inherited)
            for value in (None, "", 123, "bad\npath"):
                with self.assertRaises(ValueError):
                    cli.gateway_environment(gateway, {"cli_config_dir": value})

    def test_exec_keeps_credentials_out_of_argv(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli.shutil, "which", return_value="/test/claude"), patch.object(cli.config, "load_config", return_value=SETTINGS), patch.object(cli.config.socket, "create_connection"), patch.object(cli.os, "chdir") as chdir, patch.object(cli.os, "execvpe") as execute:
            args = ["--print", "literal prompt", "--", "-C", "untouched"]
            cli.main(["-C", folder, *args])
            chdir.assert_called_once_with(Path(folder))
            executable, argv, env = execute.call_args.args
            self.assertEqual((executable, argv), ("/test/claude", ["/test/claude", *args]))
            self.assertNotIn(TOKEN, repr(argv))
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], TOKEN)

    def test_fail_closed_and_sanitized_error(self):
        with patch.object(cli.shutil, "which", return_value="/test/claude"), patch.object(cli.config, "load_config", return_value=SETTINGS), patch.object(cli.config.socket, "create_connection", side_effect=OSError(TOKEN)), patch.object(cli.os, "execvpe") as execute, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(cli.main([]), 1)
            execute.assert_not_called()
            self.assertNotIn(TOKEN, stderr.getvalue())
        with patch.object(cli.shutil, "which", return_value=None), patch.object(cli.config, "load_config") as load, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main([]), 1)
            load.assert_not_called()

    def test_rc_rejected_under_isolated_python_without_network(self):
        for args in (["--rc"], ["--rc=session"], ["--remote-control"], ["rc"], ["remote-control"],
                     ["--verbose", "rc"], ["--verbose", "remote-control"],
                     ["--model", "test-model", "--verbose", "rc"],
                     ["--model=test-model", "remote-control"],
                     ["--debug=hooks", "rc"], ["-cv", "remote-control"],
                     ["--allowed-tools", "Read", "Edit", "--verbose", "rc"],
                     ["--verbose", "--rc"], ["--print", "prompt", "--remote-control"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "claude_cli.py"), *args], capture_output=True, text=True, env={})
                self.assertEqual(result.returncode, 1)
                self.assertIn("gateway routing disables RC", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_rc_words_in_values_or_prompts_are_forwarded_unchanged(self):
        cases = (["-p", "rc"], ["--print", "remote-control"], ["-cp", "rc"],
                 ["--model", "rc", "prompt"], ["--model=rc", "prompt"],
                 ["--agent", "remote-control", "prompt"], ["-nrc", "prompt"],
                 ["--resume", "rc"], ["-rrc"], ["--debug", "remote-control"],
                 ["--tools", "rc", "remote-control"],
                 ["--verbose", "--", "rc"], ["prompt", "rc"],
                 ["mcp", "list", "remote-control"],
                 ["--system-prompt", "--rc", "prompt"],
                 ["--unrecognized-option", "rc"])
        gateway = cli.config.Gateway(SETTINGS["gateway_urls"][0], TOKEN)
        with patch.object(cli.shutil, "which", return_value="/test/claude"), \
                patch.object(cli.config, "load_config", return_value=SETTINGS), \
                patch.object(cli.config, "select_gateway", return_value=gateway), \
                patch.object(cli.os, "execvpe") as execute:
            for args in cases:
                with self.subTest(args=args):
                    original = args.copy()
                    execute.reset_mock()
                    cli.main(args)
                    execute.assert_called_once()
                    self.assertEqual(execute.call_args.args[1], ["/test/claude", *original])
                    self.assertEqual(args, original)
