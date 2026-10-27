# Install and update Night's Watch

Follow these steps in Terminal on each Mac where you want to use Night's Watch.
The source is [our fork](https://github.com/heyNag/1password-codex-plugin).
All custom files stay in `plugins/1password/skills/nightswatch/`; upstream files
and the upstream plugin version remain unchanged.

## Prerequisites

- Codex app and a Codex CLI that supports `codex plugin`.
- 1Password desktop app, signed in to the account you intend to use.
- The official `op` CLI available on PATH.
- `/usr/bin/python3`, used by the macOS shell-lifecycle helper.

In 1Password, enable **Settings > Developer > Integrate with 1Password CLI**.
You can check the commands without accessing credentials:

```sh
codex plugin --help
op --version
/usr/bin/python3 --version
```

Night's Watch uses desktop approval and the CLI. It does not need a service
account. The upstream Environment skill is included in the same plugin and
separately requires `1password-mcp` and **Integrate with MCP clients** for its MCP
operations; installing Night's Watch does not establish that connection.

## Clone the fork

```sh
mkdir -p "$HOME/projects/forks"

git clone git@github.com:heyNag/1password-codex-plugin.git \
  "$HOME/projects/forks/1password-codex-plugin"
```

If the repository is already cloned at that path, skip the clone. To update the
checkout before its first installation:

```sh
git -C "$HOME/projects/forks/1password-codex-plugin" pull --ff-only
```

Git uses your existing SSH configuration and may request 1Password approval.
These commands pull from our fork, not directly from upstream.

## Register and install

```sh
codex plugin marketplace add \
  "$HOME/projects/forks/1password-codex-plugin" && \
codex plugin add 1password@1password-plugins
```

The plugin keeps its upstream name, **1Password Developer Tools**. **Night's
Watch** is an additional skill inside it. The install ID is
`1password@1password-plugins`.

Verify the persisted installation:

```sh
codex plugin list --marketplace 1password-plugins --json
```

The `installed` array should contain `1password@1password-plugins` with
`"installed": true`, `"enabled": true`, and a source path ending in
`projects/forks/1password-codex-plugin/plugins/1password`. The version matches
upstream, currently `0.3.0`; our skill additions do not add a version suffix.

If the marketplace name is already registered to a different location, inspect
`codex plugin marketplace list --json` and resolve that source mismatch before
installing. Do not assume a matching name points to this checkout.

## Start and end a session

Open a **new Codex chat** after installation so it loads the added skill. Say:

```text
start nightswatch
```

Codex opens a persistent terminal, runs `op signin`, and waits for your approval
in 1Password. Related credential commands then reuse that terminal. Signing in
from your own Terminal window does not authorize Codex's separate terminal.

To close the managed shell, say:

```text
end nightswatch
```

If Codex does not select the skill from the phrase, invoke it explicitly:

```text
Use $nightswatch to start nightswatch.
```

No duration is required. The monitor also closes its shell when its owning
Codex app or CLI exits, while the monitor remains able to run. Night's Watch
does not prevent sleep, change power settings, or automatically renew expired
1Password authorization. Each Mac and terminal has its own approval state.

## Update an existing installation

First use `end nightswatch` for any active watch using this installation. Then
run the following sequence. `&&` stops it if a step fails, so a failed pull does
not proceed to uninstall the existing copy.

```sh
git -C "$HOME/projects/forks/1password-codex-plugin" pull --ff-only && \
codex plugin remove 1password@1password-plugins && \
codex plugin add 1password@1password-plugins
```

Removing and reinstalling refreshes the cached plugin even when the upstream
version has not changed. It removes the installed cache, not your source
checkout. If reinstalling fails after removal, fix the reported issue and rerun
the `codex plugin add` command; do not change the manifest version to force it.

Verify again:

```sh
codex plugin list --marketplace 1password-plugins --json
```

Open a new Codex chat to use the updated skill. No application restart or model
reload is required by these instructions. The installation is machine-local;
this guide does not run `dot` commands or modify `.dotfiles`.
