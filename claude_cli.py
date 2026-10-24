"""Launch the installed Claude CLI through a configured gateway, never a direct fallback."""

from collections import deque
import importlib.util
import os
from pathlib import Path
import shutil
import sys

# -I intentionally excludes this directory from sys.path; load only our sibling helper.
_spec = importlib.util.spec_from_file_location("multi_claude_config", Path(__file__).with_name("multi_claude_config.py"))
config = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = config
_spec.loader.exec_module(config)

SUBCOMMANDS = set("agents auth auto-mode config doctor gateway help import install mcp plugin plugins project remote-control rc setup-token status ultrareview update upgrade".split())

# Argument arities from Claude's global --help. Do not guess the arity of unknown
# options: a following "rc" may be their value, not a Remote Control command.
GLOBAL_FLAGS = set("""
    --allow-dangerously-skip-permissions --ax-screen-reader --bg --background
    --bare --brief --chrome -c --continue --dangerously-skip-permissions --desktop
    --disable-slash-commands --exclude-dynamic-system-prompt-sections --fork-session
    --forward-subagent-text -h --help --ide --include-hook-events
    --include-partial-messages --no-chrome --no-session-persistence -p --print
    --replay-user-messages --restricted --safe-mode --strict-mcp-config
    --tmux --verbose -v --version
""".split())
GLOBAL_VALUES = set("""
    --agent --agents --append-system-prompt --append-system-prompt-file
    --autocompact --debug-file --effort --environment --fallback-model
    --input-format --json-schema --max-budget-usd --max-turns --model -n --name
    --output-format --permission-mode --permission-prompts --plugin-dir
    --plugin-url --remote-control-session-name-prefix --session-id
    --setting-sources --settings --system-prompt --system-prompt-file
    --system-prompt-snapshot
""".split())
GLOBAL_OPTIONAL_VALUES = set("""
    --cloud -d --debug --from-pr --prompt-suggestions -r --resume --teleport
    -w --worktree
""".split())
GLOBAL_VARIADIC_VALUES = set("""
    --add-dir --allowedTools --allowed-tools --betas --disallowedTools
    --disallowed-tools --file --mcp-config --tools
""".split())


def requests_remote_control(argv):
    """Inspect supported global options without changing the forwarded argv."""
    pending = deque(argv)
    first_operand = None
    print_mode = False
    while pending:
        arg = pending.popleft()
        if arg == "--":
            break
        if not arg.startswith("-") or arg == "-":
            if first_operand is None:
                first_operand = arg
            continue
        # Commander accepts short-option clusters and attached short values.
        if len(arg) > 2 and not arg.startswith("--"):
            short = arg[:2]
            if short in GLOBAL_FLAGS:
                pending.appendleft("-" + arg[2:])
                arg = short
            elif short in GLOBAL_VALUES | GLOBAL_OPTIONAL_VALUES:
                arg = short + "=" + arg[2:]
        option, equals, _ = arg.partition("=")
        if option in ("--rc", "--remote-control"):
            return True
        if option in GLOBAL_FLAGS:
            print_mode |= option in ("-p", "--print")
        elif option in GLOBAL_VALUES:
            if not equals and pending:
                pending.popleft()
        elif option in GLOBAL_OPTIONAL_VALUES:
            if not equals and pending and not pending[0].startswith("-"):
                pending.popleft()
        elif option in GLOBAL_VARIADIC_VALUES:
            while pending and not pending[0].startswith("-"):
                pending.popleft()
        else:
            # Leave unsupported options and their possible values to Claude.
            break
    return not print_mode and first_operand in ("rc", "remote-control")


def parse_args(argv):
    """Consume wrapper options only before the first Claude argument; retain its argv."""
    args, directory, path = list(argv), None, None
    while args:
        arg = args[0]
        if arg in ("--dir", "-C", "--config"):
            if len(args) < 2 or not args[1]:
                raise ValueError("Wrapper option requires a value.")
            if arg == "--config":
                path = args[1]
            else:
                directory = args[1]
            del args[:2]
        elif arg.startswith("--dir="):
            directory = arg[len("--dir="):]
            if not directory:
                raise ValueError("--dir requires an existing directory.")
            del args[0]
        elif (directory is None and not arg.startswith("-") and arg not in SUBCOMMANDS
              and Path(arg).expanduser().is_dir()):
            directory = args.pop(0)
        else:
            break
    if directory is not None and not Path(directory).expanduser().is_dir():
        raise ValueError("Requested working directory does not exist.")
    return path, directory, args


def gateway_environment(gateway, settings):
    env = os.environ.copy()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_PROFILE",
                "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "CLAUDE_CODE_USE_AWS", "CLAUDE_CODE_USE_CLAUDE_PLATFORM_AWS"):
        env.pop(key, None)
    env.update(ANTHROPIC_BASE_URL=gateway.url, ANTHROPIC_AUTH_TOKEN=gateway.token)
    exclusions = []
    for value in (env.get("NO_PROXY", ""), env.get("no_proxy", ""), gateway.parsed.hostname):
        for host in value.split(","):
            if host.strip() and host.strip() not in exclusions:
                exclusions.append(host.strip())
    env["NO_PROXY"] = env["no_proxy"] = ",".join(exclusions)
    if "cli_config_dir" in settings:
        directory = settings["cli_config_dir"]
        if not isinstance(directory, str) or not directory.strip() or any(ord(c) < 32 for c in directory):
            raise ValueError("cli_config_dir must be a nonempty directory path.")
        env["CLAUDE_CONFIG_DIR"] = str(Path(directory).expanduser().absolute())
    for key, value in (("API_TIMEOUT_MS", "600000"), ("MCP_TIMEOUT", "30000"),
                       ("MCP_TOOL_TIMEOUT", "600000"), ("BASH_MAX_TIMEOUT_MS", "600000")):
        env.setdefault(key, value)
    return env


def main(argv=None):
    try:
        path, directory, args = parse_args(sys.argv[1:] if argv is None else argv)
        if requests_remote_control(args):
            print("multi-claude: --rc / Remote Control is unavailable: supported gateway routing disables RC; no verified forward-proxy lane is implemented.", file=sys.stderr)
            return 1
        executable = shutil.which("claude")
        if executable is None:
            raise ValueError("Claude CLI was not found on PATH.")
        executable = os.path.abspath(executable)
        settings = config.load_config(path)
        gateway = config.select_gateway([config.Gateway(url, settings["gateway_token"]) for url in settings["gateway_urls"]])
        env = gateway_environment(gateway, settings)
        if directory is not None:
            os.chdir(Path(directory).expanduser())
        os.execvpe(executable, [executable, *args], env)
    except (OSError, ValueError):
        # Never print external exceptions: paths, socket errors, and argv can contain secrets.
        print("multi-claude: launch refused; check configuration, gateway reachability, directory, and Claude installation.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
