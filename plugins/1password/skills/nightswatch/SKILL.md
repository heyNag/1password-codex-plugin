---
name: nightswatch
description: Manage a persistent desktop-approved 1Password shell when the user says "start nightswatch" or "end nightswatch". Keep related Codex terminal commands in the same session until ended or the owning Codex process exits. No duration, service accounts, sleep prevention, or power management.
---

# Night's Watch

The user controls this skill with **start nightswatch** and **end nightswatch**.
Its job is to retain one persistent shell through Codex's terminal tools, obtain
1Password desktop approval there, and reuse that session for related authorized
commands. It stays open between tasks; no duration is required.

Night's Watch does not prevent sleep, simulate activity, manage power, supervise
other chats, or keep the model running. External sleep and network interruptions
are outside its control. It neither changes 1Password's security settings nor
promises that an open shell keeps authorization valid indefinitely.

## Start nightswatch

1. Read [session-lifecycle.md](references/session-lifecycle.md) and
   [authorization.md](references/authorization.md).
2. If this chat already has a Night's Watch ID and Codex terminal session handle,
   check the helper's status and the host tool handle. Reuse that live session
   and report it as already running; do not fall through to a fresh sign-in.
   A previous successful sign-in is historical evidence, not a reason to
   proactively reauthenticate or claim that authorization cannot expire.
   Do not send commands through another chat's session or guess a replacement
   handle from an OS PID. If the old handle cannot be resumed, report that gap
   before creating a replacement that may require fresh approval.
3. Otherwise launch `/usr/bin/python3 -B
   <skill-directory>/scripts/nightswatch.py start` with Codex's terminal tool,
   a PTY, and a short yield. The helper starts an interactive shell and monitors
   its owning Codex app or CLI process. Retain its `watch_id` and the terminal
   tool's returned session handle in this chat's working context.
4. In that terminal, run `op signin`, using `--account` when the intended account
   is known. Let the user approve in 1Password. Capture the command's exit status
   with the non-secret receipt described in the authorization reference. The
   terminal tool remaining open does not prove sign-in succeeded. Do not run a
   vault/item query as a sign-in check.
5. Report that the shell is ready and which authentication step succeeded.
   If approval is refused or unanswered, stop that credential route, retain the
   shell if useful, and report the required user action. An open shell is not
   evidence of successful sign-in. Do not retry automatically.

## Reuse the session

- Send subsequent related commands through the retained Codex terminal handle,
  such as `write_stdin` for an `exec_command` PTY. Creating a new terminal for
  each command discards the session reuse this skill is meant to provide.
- Serialize commands in that shell. Wait for a command or interactive approval
  to finish before sending the next command. Use separate execution handles for
  unrelated work that does not need this credential session.
- Reuse valid approval without repeatedly asking the user to authorize the same
  work. Existing task scope still applies; ask before using an unnamed item or
  changing the authorized account or scope.
- Leave the shell open when a task finishes or the agent becomes idle. No
  heartbeat, periodic `op signin`, secret read, or artificial activity is needed
  just to keep the shell process alive.
- If 1Password requires a new approval, report it honestly. Continue independent
  authorized work where possible. CLI sign-in does not establish Git/SSH,
  signing, or MCP approval.
- Keep secrets out of model-visible output and use trusted consumers as described
  in the authorization reference. Never claim unattended credential access from
  process liveness alone.

## End nightswatch

1. Check whether a command is still attached to the shell. Preserve progress and
   apply the task's cancellation instructions before closing it. Do not terminate
   other terminal sessions, sign the user out globally, or stop unrelated jobs.
2. Use `/usr/bin/python3 -B <skill-directory>/scripts/nightswatch.py end
   --watch-id ID` in a separate short terminal-tool call. The helper sends a
   stop request to its own monitor and closes the managed shell. Repeating end
   is safe. Check its receipt instead of merely assuming the terminal closed.
3. Report the observed end result, including any remaining process or cleanup
   uncertainty. Clear this watch's saved terminal handle only after it ends.

While it remains able to run, the monitor also closes its managed shell when the
owning Codex app or CLI exits.
It checks process start identity as well as PID. The helper's status command is
for recovery and verification; the user only needs the two start/end controls.
After a host crash or external sleep, check actual liveness before reuse.

## Stay additive

The sibling `1password-environments` skill continues to own Environment management
and mounts. Follow existing machine and repository rules for other work. Starting
or ending this skill does not install hooks, add an MCP server, change upstream
plugin files or version, edit `.dotfiles`, or configure another agent host.
