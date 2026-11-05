# AgentCaptain Codex plugin

**One captain for agents across your machines.**

This package provides the `agent-captain` skill for acting as the user's Captain
in the current Codex chat. It connects management to the same local and remote
workers as the AgentCaptain workbench; it does not start another model or select
a model provider.

The controller is a domain-expert manager, not the default hands-on engineer for
every connected project. A user objective authorizes relevant worker questions,
delegation, clarifications, follow-up, corrections and acceptance review within
that scope, without asking again for every ordinary message. Read-only limits
still apply; expanded goals, secrets, approvals and unrelated destructive or
external actions do not become authorized merely because a worker is connected.
When context is missing, ask the responsible worker. Let that worker investigate,
implement, test and check authoritative revisions or PR history; review its
evidence and request rework instead of bypassing it with direct SSH edits.

Read the selected worker's latest real output and reconcile prior delivery before
sending follow-ups, so an existing task is not accidentally dispatched again.
Send normal user-style questions and tasks, not hidden Captain protocols or worker
role changes. Reports distinguish submitted input, actual work, results awaiting
acceptance, verified outcomes and blockers. Neither submitted input nor a worker
claim proves completion; specification/revision and test/artifact evidence matter.
The human retains final decisions, and the plugin does not automatically wake an
idle management chat. See [the management contract](https://github.com/du-du-wang/agent-captain/blob/main/docs/management-session-product.md).

The management tools provide an enrolled-worker cached overview, display aliases /
pins (`update_agent`), and user-requested display reattachment to an existing
SSH tmux session (`reconnect_agent`). A reconnect never starts Codex, resumes a
conversation, replays input, restarts an external tunnel, or substitutes a new
worker if the original tmux session is missing. Every enrolled open worker is
automatically manageable; naming changes display metadata, not native identity.
Inspect live evidence for the selected worker rather than treating the
cached overview as a fresh connectivity check.

Users can opt each open SSH worker into **Auto reconnect (every 10 minutes)**
from its workbench card or detail view. The setting defaults to off and is saved
by the Hub, so it remains effective when the webpage is closed. The Hub must
keep running; sleep does not wake the computer or produce catch-up attempts.
The backend only reattaches the exact original terminal. It never starts or
resumes a model, replays a task, or restarts an external tunnel.

At the user's explicit request, use `set_auto_reconnect` with the selected
`agent_id`, `enabled`, the current `expected_terminal_id`, and a stable
`request_key`. Inspect `auto_reconnect` in `get_agent` / `list_active_agents`
for the next attempt and last result. Temporary failures confirmed before
attachment may retry with the same key; uncertain outcomes, missing tmux and
authentication/host-key problems pause for inspection. Turning the setting off
stops future attempts. Closing or replacing a terminal retires its old schedule;
the setting does not transfer to a new terminal generation. This is background
display recovery, not an automatically running Captain conversation.

`reopen_agent` is a separate **destructive, explicitly confirmed** operation,
not a reconnect fallback. First inspect the selected worker with `get_agent`;
send its exact `agent_id`, the confirmed `expected_terminal_id`, `confirmed: true`,
and a stable `request_key`. It preserves that worker's registration, name, pin
and directory but replaces its terminal ID. Only identity-proven original
terminal/foreground work is stopped and its owned tmux removed, if still present;
ambiguity fails closed. Independent managers also need creation-directory
permission for that machine and directory, even for an already enrolled worker.

The replacement is a fresh shell: AgentCaptain sends no Codex/resume command and
does not replay tasks, drafts or queued input. User-configured shell startup
files may run their own programs. Saved history remains historical evidence,
not a live binding to the new terminal; inspect and associate any new session
separately. Check `operation_state` and `recovery`: a blocked or unknown result
is not success. Reuse the same key only to inspect its retained receipt; never
blindly repeat replacement with a new key. Old-generation UI writes are rejected.

This is the **existing-chat integration**. It manages the same enrolled workers
as a Hub-created manager, without activation or per-worker grants. A Hub manager
retains independent authentication and explicit new-work directory permissions.
Starting a management conversation in the Hub UI prepares
its own MCP connection and does not require installing this package.

The source package deliberately contains no machine-specific MCP command. Its
empty `.mcp.json` makes a direct source install **skill-only**, not a connected Hub.
The repository catalog is named `agent-captain-source` so it cannot accidentally
shadow the configured `agent-captain-local` marketplace. **Do not install the source
catalog for Hub control.** Prepare the local MCP bridge before installing the
full plugin:

```sh
python scripts/configure_plugin.py
```

Run that command from the repository root. It generates a separate local
marketplace under `.runtime/plugin-marketplace/`, with a copy of this package
and the standard-library MCP bridge. The generated `.mcp.json` uses the Python
interpreter that ran the command and an absolute path to that bridge. The script
prints the marketplace registration and plugin installation commands; it does
not execute them, change Codex settings, or read any service/authentication token.

Keep the generated directory while using the installed plugin: installed plugin
caches still refer to its bridge path. Moving the clone, removing that directory,
or replacing the interpreter requires generating a new marketplace and
reinstalling from it. Existing output directories are never overwritten. For an
update, choose a new output directory, for example:

```sh
python scripts/configure_plugin.py --output .runtime/plugin-marketplace-v2
```

To upgrade an existing `ceo-hub@ceo-hub-local` installation without introducing
a second plugin identity, explicitly generate a compatibility package instead:

```sh
python scripts/configure_plugin.py --legacy-ceo-identity --output .runtime/plugin-marketplace-compat-v0.7.0
```

This keeps the plugin name `ceo-hub`, skill name `ceo-hub`, marketplace name
`ceo-hub-local`, and MCP server key `ceo_hub`, while the displayed product name
is AgentCaptain. It only creates a new retained directory: it does not inspect or
edit installed caches, `config.toml`, authentication, or running services. The
normal command generates `agent-captain@agent-captain-local`. For an existing
`pron-hub@pron-hub-local` installation, use `--legacy-pron-identity` instead;
this retains that plugin/skill/marketplace identity and the `pron_hub` server key
while presenting AgentCaptain. These old identifiers are compatibility options,
not alternate product names. The flags are mutually exclusive. Install only the
identity you intend to use, not several, to avoid duplicate tools.

Review the generated commands before registration. A running MCP process and
its discovered tool list do not hot-reload after source changes: reload that
MCP connection or reopen the controlling chat and verify `tools/list` includes
`update_agent`, `reconnect_agent`, `set_auto_reconnect` and `reopen_agent`, with server version `0.7.0`. Updating the
installed skill/manifest is separate from replacing a bridge file; local plugin
cache loading may require an app reload or restart. Do not restart the terminal
broker or a worker to refresh plugin tools.

This is a local desktop/CLI integration. It is not a hosted plugin listing and
does not make a local terminal service available to cloud clients. The existing
plugin identities and installed caches are not modified merely by
generating this package; any explicit installation is a separate user action.
