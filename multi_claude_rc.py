"""Detect Claude Code Remote Control requests in a forwarded argv, without changing it."""

from collections import deque


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
