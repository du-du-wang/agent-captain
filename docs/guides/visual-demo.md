# Visual demo · 离线展示预览

[Documentation](../index.md) · [Repository layout](../repository-layout.md) · [Brand assets](../../assets/brand/README.md)

Preview the **shipped web interface** with synthetic fixtures, without connecting a real machine or starting a model. This is a reproducible UI showcase—not a simulated successful integration test.

用同一套实际 Web UI 加载虚构数据，查看工作台与管理会话界面。不需要模型、登录、SSH 或真实服务凭据；演示内容不代表实际任务已完成。

## Run locally

From the repository root, install the terminal/browser dependencies once:

```sh
npm ci
```

Dependency installation may require network access. After dependencies are installed, the preview itself runs locally without a CDN or external service:

```sh
python scripts/preview_demo.py --port 8877
```

Open the printed loopback URL, or [the demo workbench](http://127.0.0.1:8877/#agent=demo-build%3Ademo-release-review). The page displays a **SYNTHETIC DEMO / 演示数据** banner. Use the workbench and management-display controls to explore the fixed demonstration views; they do not start a Codex conversation.

Leave the command running while viewing the page. Press **Ctrl+C** in that terminal to stop **only this demo**. If port 8877 is occupied, choose another unused port; do not stop your production service just to take a screenshot.

## Isolation and limits

- Binds only to `127.0.0.1`, separate from the normal Hub at port 8766.
- Uses fixed synthetic machines, worker records, messages, and terminal text. Displayed status, Session IDs, delivery states, and test results are fixtures, not observations of real workers.
- Does not read production configuration, runtime databases, session history, model authentication, or connection descriptors.
- Does not start a terminal broker, SSH connection, model process, or background management turn; nothing is persisted.
- Rejects task submission, terminal input, creation, reconnection, closure, settings changes, and other task/lifecycle writes on the server. UI controls for unsafe actions are disabled as an additional presentation guard.
- The allowed POST requests only read fixed fixture history or adjust this process's in-memory display geometry. A resize is not a real PTY operation.

This preview proves how the interface renders and navigates with these fixtures. It does **not** prove real dispatch, authentication, native-session association, remote availability, or cross-client handoff. Use the isolated regression suites and explicitly authorized live validation for those behaviors.

## Repository screenshots

| Asset | What it shows |
|---|---|
| [workspace-demo.jpg](../images/workspace-demo.jpg) | Light workbench overview with one row per worker across machines. |
| [worker-detail-demo.jpg](../images/worker-detail-demo.jpg) | A synthetic worker's shared terminal in the detail drawer. |
| [captain-demo.jpg](../images/captain-demo.jpg) | The synthetic management display. |

These screenshots are captures of the actual web UI served by this preview, **not AI-generated screens**. They retain the browser capture's native JPEG bytes and matching `.jpg` extensions, with no pixel edits or format conversion. All displayed machines, tasks, messages and response timings remain synthetic fixtures, not observations or successful delivery evidence from real workers. The current identity uses the selected open-ring compass; see the [brand assets and usage](../../assets/brand/README.md) and its [generation brief](../../assets/brand/generation.md).

To update the showcase:

1. Start a new preview instance with the command above. Confirm that its port is the demo port and that the synthetic-data banner is visible.
2. Capture the workbench or Captain view at a readable desktop width, retaining the demo banner. Keep typography and terminal contents legible rather than shrinking a very tall page into one image.
3. Save the relevant native JPEG capture under `docs/images/` with its matching `.jpg` extension, and inspect it for layout, clipping, and accidentally captured browser/account information. If a capture tool returns a different image format, keep its actual format and update filenames and references together rather than mislabeling the bytes.
4. Confirm that every machine, project path, conversation, and activity in the image comes from the demo fixtures. Update the image caption if the view changes.

**Never capture the real port-8766 workspace for repository publication.** Real terminals can contain private project names, hostnames, native Session IDs, drafts, credentials, or delivery records even when their current task appears harmless. Do not replace the screenshot with an AI-generated product mockup or present the fixture results as real delivery evidence.
