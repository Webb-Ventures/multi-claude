#!/bin/sh
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
exec python3 -I "$HERE/claude_cli.py" "$@"
