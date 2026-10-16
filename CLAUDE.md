# CLAUDE.md

## Project
`ico-mcp` is a universal MCP server on top of the ICO framework
(`../ico`, package `ico`). An agent builds pipelines from a registry of
typed operators, runs them, proposes new operators, and crystallizes
repeated work into stable operators via GitHub pull requests.

Read `docs/design/ico-mcp.md` before any change: it holds the decisions.

## Rules
- Every feature comes with tests. Tests never touch the network: git
  remotes are local bare repos, `gh` is replaced by a fake executable.
- All code passes `pyright` in strict mode, `ruff`, and `pytest`
  (`make ci`). Public API types must never be inferred as `Any`.
- The server never imports domain operator code; only the kernel does.
- YAGNI: implement only what the current item and its tests need. No new
  extension points or config options until there is a second real use
  case. If a design grows beyond the item, stop and ask.
- Missing functionality in ICO core is added in the `ico` repo as a
  separate small change, not worked around here.
- One roadmap item per commit (or a small series). Before each item,
  briefly propose the design and wait for approval.
- Do not push until approved.
- Code, comments, docs and commit messages in English.

## Roadmap
0. [x] ICO core prerequisite (in `../ico`): `load_operators(path, status=...)`
   overrides the status of entries loaded from that package (draft vs
   stable is decided by location). Tests there. (apriori3d/ico#23)
1. [x] Scaffold: `uv` project, `ico` as a path dependency on `../ico`
   (editable) for development, official Python MCP SDK, ruff, pyright
   strict, pytest, `Makefile` with `ci`.
2. [x] Repository layout: `ico.toml` loading and validation; empty
   `domain/` and `domain_draft/` packages, `pipelines/`, `examples/`,
   `runs/` (gitignored) as in the design doc. No `init` command: the
   repository itself is the template.
3. [x] Kernel: child process with the same interpreter (`sys.executable`), JSON-lines
   protocol, start / restart / timeout kill, env allowlist; methods
   `load`, `search`, `card`, `validate`, `run`; short type names with
   ambiguity errors. Tested without MCP.
4. [x] MCP server (stdio): tools `search_operators`, `get_operator`,
   `validate`, `run`, `save_pipeline`, `list_pipelines`, `get_pipeline`,
   `reload`; server `instructions`; run traces in `runs/`. Tested with an
   in-process MCP client.
5. [x] Progress: the kernel attaches `ico`'s `JsonProgressTool` to every
   `run` and streams its snapshots as protocol notifications; the server
   reports the first progress node of the flow as MCP progress
   (`progress` / `total` = its `done` / `total`, `message` e.g.
   "inpaint 120/900"). The SDK sends nothing without a progress token.
   The final state of every node goes into the run trace; the other nodes
   are shown by the dashboard (item 9). Needs apriori3d/ico#24 (`run`
   tools, initial snapshots on activation).
6. [ ] `propose_operator`: stages write → typecheck (pyright, file only,
   on by default) → load → register → examples, structured stage reports.
7. [ ] `crystallize`: branch, move drafts to stable, tests from examples,
   gate (pyright strict, tests, validate pipelines), commit, push,
   `gh pr create` with cards, gate report and trace stats.
8. [ ] Asynchronous runs: `run(wait=false)` returns a `run_id`;
   `run_status(run_id)` returns a compact snapshot with all progress nodes
   (key, label, done, total, rate, eta), state and error count;
   `cancel_run(run_id)`. Runs live in the kernel; the server tracks them.
9. [ ] Dashboard as an MCP App (needs the deferred ICO core JSON backend for
   `describe`; implement it in `../ico` as part of this item): a `ui://ico/run` resource
   (`text/html;profile=mcp-app`) linked from `run` and a `describe(spec)`
   tool via `_meta.ui.resourceUri`. The page renders the `describe` JSON
   view model (ICO core) generically, and polls `run_status` through
   `callServerTool` to overlay live progress on the graph nodes by
   `progress_key`. Scripts are bundled into the HTML (no CDN). Text
   results stay for clients without MCP Apps (Claude Code). Check how much
   the Python MCP SDK supports MCP Apps; otherwise set `_meta` and the MIME
   type manually.
10. [ ] Docs: how to connect the server to Claude Code (`.mcp.json`) and to
   the desktop app; README with the workflow; how to
    specialize a clone (rename `domain` / `domain_draft`, set `ico.toml`).

## Deferred
See "Deferred" in `docs/design/ico-mcp.md`. Do not implement without a
real need found in actual use.
