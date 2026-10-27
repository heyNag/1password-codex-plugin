# Persistent shell lifecycle

Use the bundled `scripts/nightswatch.py` with `/usr/bin/python3 -B`. Resolve the
script relative to this skill's installed directory rather than a hardcoded
checkout. The runtime uses Python's standard library and macOS process metadata.
It needs no Keepresso, power-management tool, server, or background service.

## Start and identify

```sh
/usr/bin/python3 -B /absolute/skill/scripts/nightswatch.py start
```

Launch with the Codex terminal tool's PTY option. The helper remains in the
foreground, runs a cleaned `/bin/zsh -f` child on that terminal, and prints a JSON
receipt containing the watch ID. The tool's separate session handle is what Codex
uses to send shell commands. Retain both identifiers in the current chat.

The helper does not call `op` or claim authentication success. The skill sends
`op signin` through the returned terminal handle and lets the user approve it.
Other related credential commands use that same handle.

Start can take `--watch-id UUID` to resume the identity of a known watch; a live
monitor with that ID must not be duplicated. `--owner-pid PID` selects a verified
owning Codex app or CLI when automatic ancestor discovery cannot identify it.
Do not select another open app just because its name resembles Codex.

Runtime files are private under `~/Library/Application Support/Nightswatch`.
They contain lifecycle and process identities, not secret values, account tokens,
or shell transcripts. The helper uses process start identity so PID reuse cannot
make an exited owner look alive.

## Verify or end

```sh
/usr/bin/python3 -B /absolute/skill/scripts/nightswatch.py status --watch-id WATCH_UUID
/usr/bin/python3 -B /absolute/skill/scripts/nightswatch.py end --watch-id WATCH_UUID
```

Use separate short terminal-tool calls for these controls. The managed PTY is an
interactive shell, so normal commands and `op signin` go through its existing
host tool handle. Status verifies live process identities; a file on disk alone
must not be treated as an active session.

End requests that the monitor close only its own child shell. It does not signal
an arbitrary recorded PID, stop other sessions, or globally log out of 1Password.
An old stop request must not affect a later instance reusing the same watch ID.
Commands attached to the shell can be interrupted by closure. Inspect the end
receipt and preserve progress before stopping a busy shell.

The same cleanup runs when the owner process exits. If the monitor is killed
abruptly, status must report stale or incomplete state rather than promising
cleanup. If an old tool session cannot be resumed, explain that before replacing
it. Sleep is allowed; no power assertions or simulated activity are created.

For isolated tests, `--state-dir PATH` selects a temporary runtime directory and
`--shell PATH` supplies a fake shell. These are test seams, not prerequisites or
user-facing duration controls.
