# Contributing to AgentCaptain

Thanks for helping make personal multi-machine agent management more reliable.
Keep contributions small, reproducible, and explicit about authority and failure
states. This project is licensed under MIT; contribute only code and assets you
have permission to share.

## Start with a small, clear change

- Use the [bug report](https://github.com/du-du-wang/agent-captain/issues/new?template=bug_report.yml)
  for a reproducible problem, or the [feature request](https://github.com/du-du-wang/agent-captain/issues/new?template=feature_request.yml)
  to describe a workflow and how success should be checked. English and Chinese
  reports are welcome.
- Check existing issues first. For a substantial change, discuss the problem and
  boundaries before investing in a large implementation.
- Report vulnerabilities through the process in [SECURITY.md](SECURITY.md), not
  an ordinary issue. Do not attach real runtime state or business conversations.

For a quick map of the source and its boundaries, see
[repository layout](docs/repository-layout.md). Keep discussion constructive,
specific, and respectful; critique the change, not the person.

## Development checks

The Python application and tests use the standard library. Install Node.js 22
and Python 3.11 or newer, then run from the repository root:

```sh
npm ci
python -m pip install -e .
python -m unittest discover -s tests -t . -v
npm run test:terminal
npm run test:ui
```

Application code lives under `src/agent_captain/`, the Node PTY service under
`terminal/`, and the browser interface under `web/`. Import Python code through
`agent_captain`, not a bare filename or a checkout-specific `sys.path` patch.
The editable install makes package imports available from unrelated working
directories. For a source-checkout launch without installation, use
`python scripts/captain.py serve`; both entry points call the same application.
See [component boundaries and migration](docs/repository-layout.md) before
changing subprocess paths, helper delivery, or installed plugin references.

`node-pty` may require the platform's native build tools when a compatible
prebuilt binary is unavailable. CI covers Windows and Linux with Python 3.11 and
3.13. Test results are evidence for those environments, not a guarantee of every
shell, tunnel, provider, or remote-machine combination.

CI runs on pushes to `main`, pull requests, and manual dispatch. Every run checks
documentation links/branding, publication hygiene, and the CI-routing rules on
Ubuntu, without installing dependencies. Changes limited to the listed root
documentation/license files, documentation Markdown/images, static brand assets,
or the pull request template stop there.
Code, configuration, tests, tooling, unknown paths, and manual dispatch keep the
full four-job matrix. Missing diff history also runs the full matrix. Superseded
runs on the same branch/PR are cancelled, and test jobs have a 20-minute timeout.
CI does not upload build artifacts. To configure a required check, use the
always-present **CI result** check: it requires successful publication checks and,
when selected, every matrix job. The existing OS/Python checks are intentionally
skipped for documentation-only changes, not left pending by a workflow-level path
filter. Publication checks must pass before tests can start.

The default suites use fake transports, temporary directories, and isolated
loopback HTTP services. Do not enable live test environment variables in CI.
Files ending in `_live.cjs` are explicitly invoked diagnostics, not part of the
normal Node suites. The opt-in Python live tests must only be used with informed
consent and disposable terminals. Never point a diagnostic at somebody else's
active work or publish its runtime files.

Leave `CEO_HUB_LIVE_TESTS`, `CEO_HUB_REMOTE_TESTS`, and
`RUN_WINDOWS_BACKGROUND_LIVE` unset or set to `0` for ordinary development. Do not
connect a real machine, start a model, or restart a user's service just to validate
a UI or documentation change.

## Before a commit

1. Add regression tests for the relevant state transitions. A successful SSH
   banner is not enough to establish a persistent terminal workflow.
2. Keep model/provider credentials, local configuration, host aliases, absolute
   personal paths, session IDs, and logs out of source and examples. Use clearly
   synthetic fixtures such as `example-ssh-host` and `/home/example/project`.
3. Inspect the diff. Stage only intended source files, then run
   `python scripts/check_publication.py --tracked` to inspect the actual Git index.
   This includes newly staged files and excludes private, unstaged deployment
   material. Do not substitute the default whole-checkout candidate scan in an
   installation that also contains local notes or project handoffs.
   Review test fixture paths manually: the checker's generic personal-path
   heuristic excludes `tests/`, but its key, host/session, and private-file rules
   still apply. Do not copy real deployment data into synthetic fixtures.
4. Document compatibility limits and any opt-in or migration step. Do not claim
   a repair is deployed or a remote session is restored without checking it.

The publication check is heuristic: it does not review Git history, binary asset
metadata, Actions logs, or screenshots. Review those separately before sharing.
Never stage a private file merely to make it part of a check.

## Opening a pull request

Explain the user-visible problem, the change, and the checks actually run. Keep
unrelated cleanup separate. Use the pull request checklist to call out security,
compatibility, or manual validation limits, including checks you could not run.
An issue, screenshot, or passing unit test is not evidence of a live deployment.

For documentation-only changes, check links, examples, and rendering; mark
unrelated suites as not run with a reason. For behavior changes, add a regression
test and run the relevant suites. Maintainers may ask for a smaller reproduction
or more evidence; no review turnaround time is promised.

## Reliability and safety requirements

- Preserve existing sessions and other users' changes during reconnects.
- Keep remote transports behind the SSH interface; tunnel-provider-specific
  integration belongs in the independent transport tool.
- Do not automatically restart shared background services, fork locked sessions,
  delete session locks, approve startup menus, or change account/provider settings.
- Do not silently resend terminal input after an ambiguous delivery. Preserve
  stable request identifiers and verify a delivery before deciding to retry.
- Distinguish a terminal screen snapshot from persisted conversation output and
  make pagination or incomplete reads visible.
- Keep HTTP/broker authorization and loopback/same-origin checks intact. Avoid
  logging tokens, environment variables, full process arguments, or transcripts
  merely to debug a connection.

Security-sensitive reports should follow [SECURITY.md](SECURITY.md). Include
platform versions and a sanitized minimal reproduction for ordinary bug reports.
