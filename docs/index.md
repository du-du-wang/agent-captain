# AgentCaptain documentation · 文档导航

[English home](../README.md) · [中文首页](../README.zh-CN.md)

Start with what you want to do. The guides describe real behavior and its boundaries; they are not a promise of unattended operation or universal Codex compatibility.

按任务找入口：安装、日常管理、故障恢复、参与贡献。文档描述实现与边界，不把演示或单元测试当作所有环境下的实测保证。

## Get started · 开始使用

Want an agent to handle setup? Copy the [English setup prompt](../README.md#get-started) or [中文安装提示词](../README.zh-CN.md#快速开始) into your coding agent. 它会按指南协助安装、验证工作台并引导你启动第一个管理会话；手动安装步骤也保留在同一入口。

- [English operator guide](guides/operator-guide.md) / [中文操作指南](guides/operator-guide.zh-CN.md) — requirements, installation, first Captain, and worker connections.
- [Management session: design and limits](management-session-product.md) — what the Captain owns, how identity is established, delegation and acceptance, and experimental desktop transfer.
- [Existing-chat integration](guides/operator-guide.md#optional-connect-an-existing-codex-chat-with-the-legacy-plugin) / [已有对话接入](guides/operator-guide.zh-CN.md#可选用旧版插件接入已有-codex-对话) — optional compatibility path, not a prerequisite for a Hub-created Captain.
- [Security model](../SECURITY.md) — loopback service, private data, authentication, and the different creation boundaries of independent managers and legacy service credentials.
- [Offline visual demo / 离线展示预览](guides/visual-demo.md) — explore the actual web UI with synthetic data, no real workers, models, or credentials.

## Daily work · 日常管理

- [Agent workbench](agent-workbench.md) — name, filter, pin, open, and reconnect registered work; terminal scrolling, human drafts, and safe closure.
- [Message times and SSH response](guides/operator-guide.md#message-times-and-ssh-response) / [消息时间与 SSH 响应](guides/operator-guide.zh-CN.md#消息时间与-ssh-响应) — distinguish a recorded terminal write from model/task acceptance and passive SSH response samples from ping.
- [Read output without guessing](guides/operator-guide.md#read-output-without-guessing) / [会话输出不是终端截图](guides/operator-guide.zh-CN.md#会话输出不是终端截图) — exact Session association, pagination, completeness flags, and live versus historical evidence.
- [Manage an objective, not every message](guides/operator-guide.md#manage-an-objective-not-every-message) / [交代目标，不必逐条批准管理消息](guides/operator-guide.zh-CN.md#交代目标不必逐条批准管理消息) — workers execute, the Captain coordinates and reviews, and the human makes final decisions.
- [MCP tool reference](guides/operator-guide.md#mcp-tools) / [MCP 工具](guides/operator-guide.zh-CN.md#mcp-工具) — tool purposes and the permissions they actually enforce.

## Recover and maintain

恢复之前先区分断的是网页服务、显示连接、SSH 路由，还是模型本身。**重新连接显示不等于启动或恢复 Codex，也不应重发项目任务。**

| Symptom / 现象 | Start here / 入口 |
|---|---|
| Web page unavailable; service status uncertain | [Background service lifecycle](background-service.md) |
| Page works, but a worker display is disconnected | [Workbench recovery contract](agent-workbench.md) |
| Terminal screen works, but independent session reads fail | [SSH transport and concurrent channel requirements](ssh-transport.md) |
| CLI updated, but its shared daemon or features differ | [Codex maintenance](codex-maintenance.md) |
| Upgrading an existing deployment after the product rename | [Brand and compatibility notes](rebranding.md) |
| Upgrading from the old flat source tree | [Source layout and local compatibility entries](repository-layout.md#existing-installation-migration) |

An SSH banner or a single successful command is not proof that persistent display and concurrent history reads both work. External tunnel adapters own their endpoint recovery. A lost original terminal requires an explicit choice, not silent replacement.

## Contribute · 参与贡献

- [Contribution workflow](../CONTRIBUTING.md) — setup, offline tests, review expectations, and safe publication checks.
- [Report a bug](https://github.com/du-du-wang/agent-captain/issues/new/choose) — include a reproducible example and redact sensitive output before attaching it.
- [Security reporting](../SECURITY.md) — never put credentials or unredacted runtime data into an ordinary issue.
- [Repository layout](repository-layout.md) — component boundaries and where source, documentation, and private installation state belong.
- [Brand assets and usage](../assets/brand/README.md) — canonical logo, typography, palette, and generation provenance.
- [MIT license](../LICENSE) and [third-party notices](../THIRD_PARTY_NOTICES.md).

Python implementation lives in the layered `src/agent_captain/` package; the PTY broker and browser interface have separate `terminal/` and `web/` directories. Runtime state, connection descriptors, model authentication, and real session records belong outside version control—not in the documentation or showcase.
