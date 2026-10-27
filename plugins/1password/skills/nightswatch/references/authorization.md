# Desktop approval in a persistent terminal

Use the user's established 1Password account and the credentials named by the
task. Before using an additional item, identify it and its purpose and obtain the
user's permission. An already-approved terminal does not expand task scope.

## Establish the session once

The lifecycle helper creates a child shell with
`OP_BIOMETRIC_UNLOCK_ENABLED=true`, without inherited
`OP_SERVICE_ACCOUNT_TOKEN`, `OP_CONNECT_HOST`, `OP_CONNECT_TOKEN`, or any
`OP_SESSION*` variables. It changes only that child environment, starts the shell
without startup files, and never prints its environment. Use no service accounts,
Connect configuration, token exports, or separate agent vault.

Run `op signin` in that managed PTY, with `--account` when the intended account
is known. Leave account selection and secure approval to the user. Do not use
`--raw`, `--force`, command substitution, or a broad vault-list/`op whoami`
preflight. Version/help checks do not establish authentication. Check the
sign-in's exit status and retain the Codex tool session handle for subsequent
related commands. Never confuse it with a shell PID or the helper's watch UUID.

Submit sign-in and a non-secret exit marker as one shell command. Choose a fresh
marker suffix for the attempt so old output cannot be mistaken for its receipt:

```sh
op signin; printf '\nNIGHTSWATCH_SIGNIN_ABC123=%s\n' "$?"
```

Add a properly shell-quoted `--account` argument when needed. Wait for that
attempt's marker; a 0 value establishes that this sign-in returned successfully.
The shell tool will still be running because its session is intentionally open.

Once approval is valid, use it for the already-authorized work without repeatedly
asking the user to sign in. An idle shell stays alive without credential polling.
The desktop CLI's documented authorization is per account and terminal session:
it expires after 10 minutes of inactivity, refreshes with use, and has a 12-hour
hard limit. Locking 1Password revokes it. A process already running at revocation
can finish, but a new invocation may require approval. Shell persistence and
credential authorization are separate states.

Do not create periodic sign-in or credential-reading jobs to defeat expiry,
simulate activity, automate secure approval, or alter lock or power settings.
If the user wants a different security policy, treat that as a separate request.

## Use credentials through the approved shell

- Use `op run --environment ID -- trusted-command` for an existing Environment,
  or reference-only variables/templates with `op run` for named vault credentials.
  Batch already-authorized references when they belong to the same trusted job.
  Check installed help before using flags. Deliver values to the consumer, never
  to a diagnostic command or the model context.
- A trusted job may receive its required credentials at launch. `op run` gives
  its child environment variables for that process's lifetime. This does not
  authenticate later unrelated tool calls or prevent external token expiry.
  Do not preload a broad collection of secrets into the persistent shell.
- Inspect dotenv precedence before claiming that injected values are used.
  Check existing `.env` metadata without opening a secret file or FIFO. Keep
  masking enabled and inspect unfamiliar consumers before supplying secrets.
- Use `op inject` only when an authorized configuration consumer requires it,
  with restricted permissions and cleanup for plaintext output. Keep resolved
  output out of chat, logs, source control, and generic temporary files.
- Preserve the native SSH agent, signing identity, host verification, and remote.
  `op signin` does not approve SSH key use or Git signing. Perform the actual
  authorized Git operation, not a dummy commit/push to obtain approval.
- Use the sibling Environment skill for MCP operations. A terminal approval does
  not establish MCP approval.

## When approval is needed again

Stop the credential-dependent operation on a refused or unanswered approval.
Do not repeat it, switch accounts/tools, or change settings to get around it.
Record the pending operation without secret values, continue independent work,
and report the specific action needed from the user. Reconcile an uncertain
write before another attempt after the user resumes.

A pending foreground command still owns the terminal. Do not send another shell
command into its prompt. If the user has declined or is away and cannot respond,
interrupt only that pending operation when safe, then wait for the shell to
return. If cancellation is uncertain, mark the terminal busy and use another
handle for independent work. This does not mean ending the watch or retrying
the credential request.

Keep the shell open for reuse until `end nightswatch`, shell exit, or owner exit;
a blocked task does not itself end the watch. Report shell readiness, successful
sign-in, actual credential use, and consumer success separately. A background or
pending approval must never be described as completed authentication.

## Sources

- [CLI desktop integration](https://www.1password.dev/cli/app-integration)
- [CLI authorization lifetime and terminal scope](https://www.1password.dev/cli/app-integration-security)
- [Secret delivery with op run](https://www.1password.dev/cli/reference/commands/run)
- [SSH authorization](https://www.1password.dev/ssh/agent/authorization)
- [Environment MCP approval](https://www.1password.dev/environments/mcp-server)
