# AgentCaptain rename and compatibility / 改名与兼容

The product is **AgentCaptain**. Its positioning is **One captain for agents across your machines. / 一个主控，统筹本机与远程 Agent。** The native management model is the **Captain**; the coding agents doing the work are **Workers**. The human keeps final decision-making authority.

This is a branding update, not a new remote-control protocol or permission model. Task delegation, progress follow-up, delivery review, direct worker terminals, and local/SSH connections retain their existing boundaries. It does not add background model wake-ups, automatic task replay, or unattended recovery.

## New installations

- The product and recommended checkout directory are named `AgentCaptain` / `agent-captain`; use `run_agentcaptain.cmd` for the Windows launcher.
- The generated existing-chat plugin is `agent-captain@agent-captain-local`. The source-only template is `agent-captain-source`, not a configured live connection.
- New production management workspaces use `%LOCALAPPDATA%/AgentCaptain/management-workspaces/` on Windows and `$XDG_STATE_HOME/agent-captain/management-workspaces/` on Linux/macOS (default `~/.local/state/agent-captain/management-workspaces/`). Explicit development/test runtimes remain isolated in their chosen namespaces.

## Repository URL and publication status

The canonical GitHub repository is [du-du-wang/agent-captain](https://github.com/du-du-wang/agent-captain). New checkouts can use `git clone https://github.com/du-du-wang/agent-captain.git agent-captain`. Version 0.7 is a checkout-based source release under the [MIT license](../LICENSE), not a standalone installer or a PyPI/npm distribution.

Source publication does not expose a user's local runtime, settings, credentials, or saved sessions. Updating a checkout is not evidence that its changes have been committed, pushed or released. Repository visibility and release publication are separate hosting actions, not side effects of a branding migration. Experimental management-session and desktop-transfer boundaries remain unchanged.

项目统一使用 [du-du-wang/agent-captain](https://github.com/du-du-wang/agent-captain)；0.7 是 MIT 许可的源码版本，不是独立安装器或 PyPI/npm 包。公开源码不包含用户的本地运行数据、凭据或会话。本地修改不代表已经提交、推送或发布，仓库改名也不会自行改变可见性；管理会话和桌面交接的实验性边界不变。

## Existing installations: preserve identities and paths

Earlier releases used the product names **PRON Hub** and **CEO Hub**. Those names are no longer the public brand. Some technical identifiers intentionally remain compatible:

| Existing item | Compatibility behavior |
| --- | --- |
| Checkout folder such as `ceo_hub` | A folder name is not a worker identity. Do not move a running installation merely to change its visible brand; absolute-path launch and plugin references would need deliberate regeneration. |
| `CEO_HUB_*` environment variables; `ceo_hub` / `ceo-hub` service or protocol identifiers | Existing overrides and clients remain valid. These are compatibility identifiers, not the new product name. |
| Terminal IDs starting with `ceohub-`, native Session IDs, request keys and delivery records | Preserved exactly. No replacement shell, model resume, task replay or uncertain-operation reset is part of this rename. |
| Saved management workspaces under `PRONHub`, `pron-hub`, `CEOHub` or `ceo-hub` | Existing records keep their exact saved workspace paths. Only newly prepared production workspaces use the new default; no automatic move, deletion, re-trust or session recreation. |
| `run_agentcaptain.cmd` | The only supported Windows launcher. Older `run_pron.cmd` and `run_ceo.cmd` aliases have been removed. |
| Installed `pron-hub` / `ceo-hub` plugins and machine-local marketplace caches | Do not delete a working cache or rewrite it in place. Generate a new output directory using `scripts/configure_plugin.py`, follow the printed install commands, and verify the new tool connection before retiring an old entry. Avoid enabling duplicate management entries. |
| `.runtime/` state, descriptor references and saved host aliases | Remain private and are not renamed blindly. Worker and Captain identities, tokens and request namespaces are not copied into source, prompts or public migration notes. |

An already running Codex chat retains its loaded tool context. Renaming source files, generating a marketplace or restarting the web service does not prove that chat has loaded the new plugin metadata. Select the updated plugin in a new chat or reload only when the client requires it; do not resend business tasks just to test the rename. A saved management workspace's guidance is not silently rewritten while its conversation is running.

## 中文提示

本次改名统一的是产品、界面、介绍、启动入口和新插件的名称，不是重建你已有的工作。已登记的机器、别名、原终端、准确 Codex Session、管理身份、工作区路径和未知送达记录都应保留。

旧目录名、环境变量和协议字段是为了兼容已有安装而存在，不需要用户在正常操作中理解它们。不要为“清除旧名”强删运行目录、插件缓存或锁，不要结束正在工作的 agent，也不要靠新建会话或重发任务掩盖迁移问题。

If a check fails or the original state is uncertain, stop at that explicit state and reconcile the original identity. See [security boundaries](../SECURITY.md), [the workbench contract](agent-workbench.md), and [background service lifecycle](background-service.md).
