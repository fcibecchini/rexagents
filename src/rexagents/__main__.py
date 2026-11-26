"""Command line entry point.

`claude agents` takes no positional arguments, so a leading word is always a
rexagents command and leading flags all go to `claude agents`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import __version__

HELP = """\
rexagents: the stock `claude agents` view, opening each session in its own
Rex pane, with the session's status in Rex's Activity sidebar.

Usage:
  rexagents [claude agents options]   run the agent view
  rexagents hooks install             make every background session report its status
  rexagents hooks uninstall           take the hooks out again
  rexagents hooks                     print the hook settings, for --settings
  rexagents doctor                    check that everything rexagents needs is in place
  rexagents help [command]            show this help, or a command's (same as --help)
  rexagents version                   print the version (also -v, --version)

Examples:
  rexagents                           every session; Enter or → opens one in a new pane
  rexagents --cwd .                   only sessions started in this directory
  rexagents hooks install --dry-run   see what installing changes in ~/.claude/settings.json
  claude --bg --settings "$(rexagents hooks)" "your prompt"
                                      one session that reports, without installing the hooks

In a session's pane, ← with an empty prompt goes back to the agent view.
Options other than --help, --version and -v go to `claude agents`; see
`claude agents --help`.
"""

COMMANDS = ("help", "version", "hooks", "doctor", "attach")


def _docs() -> str:
    readme = Path(__file__).resolve().parents[2] / "README.md"
    return f"Docs: {readme}\n" if readme.is_file() else ""


def _parser(command: str, description: str):
    import argparse

    return argparse.ArgumentParser(prog=f"rexagents {command}", description=description)


def _hooks(args: list[str]) -> int:
    parser = _parser("hooks", "Install or remove the Claude Code hooks that report each background "
                              "session's status, or print them (with no action) for --settings.")
    parser.add_argument("action", nargs="?", choices=("install", "uninstall"))
    parser.add_argument("-y", "--yes", action="store_true", help="don't ask before changing the settings")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show the change without making it")
    opts = parser.parse_args(args)
    if not opts.action and (opts.yes or opts.dry_run):
        parser.error("--yes and --dry-run go with install or uninstall")

    import json

    from . import hook
    from .install import edit, with_hooks, without_hooks

    path = hook.user_settings()
    if opts.action == "uninstall":
        return edit(path, without_hooks, summary="this removes the rexagents hooks from",
                    done=f"removed the hooks from {path}.", unchanged="there are no rexagents hooks in",
                    dry_run=opts.dry_run, yes=opts.yes)
    launcher = hook.launcher()
    if not launcher:
        print("rexagents: can't find the rexagents command on PATH. Install it first (see the README).",
              file=sys.stderr)
        return 1
    ours = hook.settings(launcher)
    if not opts.action:
        print(json.dumps(ours, indent=2))
        return 0
    return edit(path, lambda config: with_hooks(config, ours),
                summary=f"this adds the rexagents hook to {len(ours['hooks'])} events in",
                done=f"installed the hooks in {path}. Background sessions report their status "
                     "from the next time their worker starts.",
                unchanged="the hooks are already installed in", dry_run=opts.dry_run, yes=opts.yes)


def _doctor(args: list[str]) -> int:
    _parser("doctor", "Check that everything rexagents relies on is in place; exits 1 if something "
                      "needs fixing.").parse_args(args)
    from . import doctor

    return doctor.run()


def _version(args: list[str]) -> int:
    _parser("version", "Print the version.").parse_args(args)
    print(f"rexagents {__version__}")
    return 0


def _help(args: list[str]) -> int:
    if not args or args[0] == "help":
        print(HELP + _docs(), end="")
        return 0
    if args[0] not in _COMMANDS:
        return _unknown(args[0])
    return _COMMANDS[args[0]]([*args[1:], "--help"])


def _unknown(command: str) -> int:
    import difflib

    close = difflib.get_close_matches(command, COMMANDS, n=1)
    hint = f" Did you mean 'rexagents {close[0]}'?" if close else ""
    print(f"rexagents: unknown command '{command}'.{hint}\nRun 'rexagents help' for usage.", file=sys.stderr)
    return 2


def _attach(args: list[str]) -> int:
    parser = _parser("attach", "Internal: run `claude attach` in a pane opened by the agent view.")
    parser.add_argument("session_id")
    parser.add_argument("--title", required=True, help="session name, for the status report")
    parser.add_argument("--agent-view", help="block to focus when ← goes back")
    parser.add_argument("--cwd", help="a stopped session's directory, to check it can wake")
    opts = parser.parse_args(args)

    from . import agentview
    from .proxy import ask, run_attach

    agentview.show_as_claude(["attach", *args])

    session_id = opts.session_id
    from .rexpane import location

    rex_session, block = location() or (None, None)
    if opts.cwd is not None:
        from . import recover
        from .agentview import _rex, _sessions

        session_id = recover.preflight(session_id, opts.cwd, ask, lambda: _sessions([]))
        if session_id is None:
            return 0
        if session_id != opts.session_id and rex_session and block:
            _rex("block", "rename", "-s", rex_session, block, f"claude:{session_id[:8]}")

    on_input = None
    if opts.agent_view and rex_session and block:
        from .agentview import _rex, attach_input

        on_input = attach_input(rex_session, block, opts.agent_view, _rex)
    return run_attach(session_id, opts.title, ["claude", "attach", session_id[:8]], on_input)


_COMMANDS = {"help": _help, "version": _version, "hooks": _hooks, "doctor": _doctor, "attach": _attach}


def main(args: list[str]) -> int:
    if args == ["hook"]:  # run by Claude Code on every hook event: import as little as possible
        from .hook import main as hook_main

        return hook_main()
    if not args or args[0].startswith("-"):
        if "-h" in args or "--help" in args:
            args = ["help"]
        elif "--version" in args or "-v" in args:
            args = ["version"]
        else:
            from . import agentview

            return agentview.run(args)

    command, rest = args[0], args[1:]
    if command in _COMMANDS:
        return _COMMANDS[command](rest)
    return _unknown(command)


def cli() -> None:
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
