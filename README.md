# rexagents

Run Claude Code's agent view (`claude agents`) in [Rex](https://www.superlogical.com/rex)
and open each background session in its own Rex pane. Every session pane reports its status to Rex's Activity panel, so you can see at a glance which session needs you.

## Requirements

- [Rex](https://www.superlogical.com/rex)
- [Claude Code](https://code.claude.com) 2.1.295 or later (background sessions
  and the agent view)
- Python 3.9 or later
- macOS

## Install

Install it with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install -e .   # puts rexagents in ~/.local/bin
rexagents --version
```

Then install the hooks that report each session's status:

```sh
rexagents hooks install --dry-run   # show the change to ~/.claude/settings.json
rexagents hooks install             # installs and keeps a backup
```

Check the result with `rexagents doctor`.

<details>
<summary>What installing the hooks changes</summary>

The hooks are added to `~/.claude/settings.json` after your own.
They reach every background session however it was started, including
sessions created before you installed them, from the next time their worker
starts (for example when you open a stopped session).

If you'd rather not edit your settings, skip this step: `rexagents` then
starts the agent view with `--settings` holding the hooks, and Claude Code
passes them on to the sessions you start from it. Only those sessions report,
and the agent view turns `/resume` off. If you pass your own `--settings`,
`rexagents` leaves it alone. To try the hooks on a single session:

```sh
claude --bg --settings "$(rexagents hooks)" "your prompt"
```

</details>

## Quick start

In a Rex pane, run:

```sh
rexagents              # every background session
rexagents --cwd .      # only sessions started in this directory
```

Any option you'd give `claude agents` works the same way, for example
`rexagents --model opus`. Then open a session with Enter, and press ← in its
pane to go back to the list.

## Commands

| Command | What it does |
| --- | --- |
| `rexagents [claude agents options]` | Run the agent view |
| `rexagents hooks install` | Add the status hooks to `~/.claude/settings.json` (`--dry-run` to preview, `--yes` to skip the question) |
| `rexagents hooks uninstall` | Take them out again |
| `rexagents hooks` | Print the hook settings, for `--settings` |
| `rexagents doctor` | Check that everything rexagents needs is in place |
| `rexagents help [command]` | Show help |
| `rexagents version` | Print the version |

## How it works

**Opening a session.** `rexagents` runs `claude agents` behind a
pseudo-terminal proxy that sees every key and mouse click first. When one
would attach a session:

1. It reads the agent view's screen from Rex (`rex capture --format vt`).
2. It checks the footer says Enter opens a session (`enter to open`,
   `enter to return` or `enter to resume`). Otherwise the key means something
   else, such as sending a reply, and goes through.
3. It finds the row: the selected one (highlighted by the arrow keys, or drawn
   brighter under the mouse), the Nth one, or the one clicked.
4. It matches the row to exactly one session in
   `claude agents --json --all`, by name. A name cut short with `…` matches by
   prefix. A new session shows its prompt until Claude names it, so if no name
   matches, the one running unnamed session is used. If several sessions share
   the name, the row's summary (its result, its question, or `stopped`) is
   compared with each session's job state; a row with no summary is the one
   that's working.
5. It swallows the key and runs
   `rex split -- rexagents attach <session id>`. Panes are labelled
   `claude:<id>`, which is how an open session's pane is found again.

**Going back.** The session pane runs `claude attach` behind the same kind of
proxy. On ←, Ctrl+C or Ctrl+D it reads the pane's screen, and if Claude is
about to go back to the agent view, it focuses the real agent view and closes
the pane instead.

**Reporting status.** Claude Code turns status reporting off for background
sessions, even while attached, and a hook's output can't carry OSC 7501.
So the hooks inside each session record its status in
`~/.local/state/rexagents/<session id>/status.json` (or under
`$XDG_STATE_HOME`) and write it to a fifo each open pane listens on. The
session's pane replays the last status when it starts, then writes each new
one into its own terminal as an
[OSC 7501](https://www.superlogical.com/rex/docs/build/program-status) report,
only between escape sequences and complete characters. Each report carries
the session's current name, which the pane also checks every 2 seconds, so a
session Claude names after its last status still gets its name in the panel.

## Troubleshooting

`rexagents doctor` checks that Rex and a recent enough Claude Code are there,
that the hooks are installed and point at an existing `rexagents`, and lists
running sessions that have never reported a status. It exits 1 when something
needs fixing, and says how.

When a key that should open or close a pane doesn't, turn on the debug log:

```sh
touch ~/.local/state/rexagents/debug.log
```

While the file exists, the agent view, the session panes and the hooks append
a line for each decision: the footer and row they read, how many sessions
matched, what they did, and any error a hook swallowed. Including the relevant
lines makes a bug report much easier to act on.

## Limitations

- **It reads the screen.** Which row is selected, whether a dialog is open and
  which session a row is aren't available any other way, so rexagents reads
  the footer, how rows are drawn and the list's layout, and matches rows to
  sessions by name and summary. A Claude Code update that changes these can
  break opening panes; rexagents then falls back to the normal inline attach.
  The same happens when two sessions share both name and summary, or when two
  new sessions haven't been named yet.
- **Interrupting a turn** with Esc fires no hook, so the pane shows `working`
  until the next event.
- **Sessions without the hooks** never report. With the hooks only in
  `--settings`, that's any session started some other way, even after a
  restart, because Claude restarts a session with the flags it was created
  with. With the hooks installed, a session that was already running starts
  reporting once its worker restarts (`claude stop`, then open it).
- **In a pane too short to show the prompt**, ← closes the pane even if you
  had typed something, because the text can't be seen.
- **Pane sizes**: Rex's "Autobalance splits" setting can override the split
  ratio.

## Uninstall

```sh
rexagents hooks uninstall
uv tool uninstall rexagents
rm -r ~/.local/state/rexagents
```

## License

[MIT](LICENSE)
