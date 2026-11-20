---
name: agent-captain
description: 在当前 Codex 对话中担任主控，通过 AgentCaptain 管理已接入的本机和 SSH 工作 agent，完成问询、派发、跟进和验收。默认委派实际项目工作，无需额外激活，不另运行主控模型。
---

The model in this Codex conversation is the manager. The AgentCaptain MCP server's instructions are the management guidance; follow them. This skill adds only setup notes and a short loop. The user keeps the final decision on everything.

## Setup

The source plugin has no machine-specific MCP launch config. Run `python scripts/configure_plugin.py` as the project README describes and install the plugin from the generated local marketplace; keep the generated directory. A plugin install error does not mean workers are offline. Do not edit the user's Codex config or copy service credentials.

## Loop

1. `list_active_agents` once to see who exists (compact rows; cached status).
2. For the worker you need: `read_agent` for its latest reply. Check `content_complete`; follow `next_cursor` if a reply is split.
3. `send_agent_message` with an English brief written the way the user would write it.
4. `wait_for_agent` with the returned `request_id`. On `timed_out`, call it again; do not poll or ask the worker for status.
5. Review the deliverable, send a targeted follow-up if needed, then report to the user.

Use `get_agent` only when you need live connection or input state, and `verbose=true` only when a compact view lacks a field you need.
