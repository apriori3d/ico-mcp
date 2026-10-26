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
6. [x] `propose_operator`: stages write → typecheck (pyright, file only,
   on by default) → load → register → examples, structured stage reports.
7. [x] Spec and progress fixes found in a live run:
   - add a short spec example to the server `instructions` (leaf
     operators use `"op": "name@version"`; an agent guessed `ref` first);
   - `progress` and `stream` nodes in a seq give "signature could not be
     inferred" warnings that name internal methods (`_identity_fn`,
     `_stream_fn`): infer the identity signature of `IcoProgress` from its
     neighbours and use node labels in messages (fix in `../ico`: untyped identity nodes
     are skipped by link checks, apriori3d/ico#25).
8. [x] Domain types. Types are plain Python classes used in operator
    signatures; today a type is known only if a registered signature uses
    it, and a module without operators is rejected. Add:
    - `propose_types(module, code)`: a types-only module in the draft
      package, stages write → typecheck → load (no register/examples);
      it must define at least one public class. Operator modules import
      types from it. Types move to the stable package with the operators
      that use them on `crystallize`.
    - `list_types` / `get_type(name)`: types of the domain packages
      (draft and stable) with module, status, docstring and fields
      (dataclass / Pydantic fields with their types), whether or not a
      signature uses them yet. Short names resolve as for search; an
      ambiguous name lists candidates.
    - Server `instructions`: look for an existing type before creating
      one; types are immutable dataclasses or Pydantic models with a
      docstring; large data at the pipeline boundary is passed by
      reference (e.g. a path in an `ImageRef`), in-memory data inside the
      pipeline is fine.
    - Import rule, so that crystallization never rewrites imports:
      draft → draft imports are relative (`from .frames import Frame`),
      draft → stable imports are absolute (`from domain.frames import
      Frame`). `propose_types` and `propose_operator` reject absolute
      imports of the draft package (stage `write`, with the relative form
      as the fix). State the rule in the server `instructions`.
    No shared standard type library yet: the vocabulary grows from real
    domains through crystallization.
9. [x] Dashboard: host the ICO Live Board (item 16 of the `ico` roadmap;
   needs it first) in the server process, not in the kernel, so the URL
   and run history survive kernel restarts. Its data source is the
   server's registry of active and recent runs, fed by the kernel's
   progress notifications (also for synchronous `run`), plus the
   `describe` JSON of each run's spec. A `dashboard` tool returns the URL;
   `run` results include it. In the desktop app it can open in the
   built-in browser pane. (MCP progress notifications were not shown by
   the desktop app, neither in the Code tab nor in a chat.) Needs
   apriori3d/ico#29 (run summary) and apriori3d/ico#30 (chain signatures
   across untyped progress nodes).
10. [x] Asynchronous runs: `run(wait=false)` returns a `run_id`;
   `run_status(run_id)` returns the same compact snapshot the dashboard
   uses; `cancel_run(run_id)`. Each run lives in a kernel process of its
   own (ICO cannot interrupt a flow, so cancel kills it); the server
   tracks them.
11. [x] `crystallize`: branch, move drafts to stable, tests from examples,
   gate (pyright strict, tests, validate pipelines, and no import of the
    draft package from the stable package); moved modules are never
    rewritten: the closure of their relative imports moves with them, and
    remaining drafts that import a moved module get that import rewritten
    to the absolute stable form, commit, push,
   `gh pr create` with cards, gate report and trace stats. Done in a
    temporary git worktree; drafts stay until the merge, then `reload` (and
    server start) retires identical ones and reports differing ones.
12. [x] Docs: how to connect the server to Claude Code (`.mcp.json`) and to
   the desktop app; README with the workflow; how to
    specialize a clone (rename `domain` / `domain_draft`, set `ico.toml`).
    Plus the agent guide (now `src/ico_mcp/agent-guide.md`; spec reference, errors, writing operators,
    runs, domain notes), served by the `guide` tool.
13. [x] Pipeline parameters (items 4, 5 and the titles of item 6 of
    `docs/proposals/2026-10-17-first-domain-session.md`; needs
    apriori3d/ico#36): `{"$param": ...}` references in specs; `run`
    by `spec` or saved `pipeline` name with `params`; `validate` and
    `save_pipeline` take `params` and report the declared parameters;
    saved pipelines keep `check_params` for validation (also in the
    crystallize gate); an explicit JSON schema for `input`; run titles
    and traces carry the pipeline name and parameter values.
14. [x] Server status (item 8 of the first domain session proposal): a
    `server_status` tool and a `server` block in `reload` compare a
    fingerprint of the server's code (`ico_mcp`, `ico`) at start with the
    disk; `stale` tells the agent to ask for a reconnect. The instructions
    start with the version and commit.
15. [x] `delete_pipeline(name)` (item 13 of the same proposal): removes a
    saved pipeline; `not_found` lists the saved ones.
16. [x] Edits of draft modules (item 9 of the same proposal):
    `propose_operator` / `propose_types` take `edits=[{"old", "new"}]`
    instead of `code` (each `old` exactly once, applied in order, a failed
    edit changes nothing); `get_operator` gives a draft's
    `module_source`.
17. [x] Examples only for pure operators (item 11 of the same proposal,
    revised): an example whose output is not JSON explains that domain
    operators need no examples; the crystallize PR shows each operator's
    recent successful runs from the traces as the evidence.
18. [x] Environment (items 7 and 7a of the same proposal): an
    `environment` tool reports what operators see in the kernel (Python,
    PATH, allowed variables without values, timeouts, packages with a
    filter, packages installed by several distributions, binaries of
    `bin:<name>` capabilities); `get_operator` shows `bins`; `validate`
    warns `missing_binary`. Guide: binaries as params, lazy imports of
    heavy frameworks.
19. [x] Draft fingerprints (item 10 of the same proposal): draft cards
    carry the fingerprint of their module; run traces record it per
    draft ref; the crystallize PR marks runs on earlier draft code. A new
    proposal of a module reports changed / removed operators with the
    saved pipelines using them and re-validates those pipelines (it does
    not fail on them).
20. [x] A run's kernel loads only the modules of the operators its spec
    uses (asked from the main kernel; everything when it cannot tell), so
    unrelated heavy frameworks stay out of it. Needs apriori3d/ico#39
    (`load_operators(..., modules=...)`).
21. [x] `wait_run(run_id, timeout)`: waits on the server until a run ends
    or the timeout (1 to 600 s) passes, and returns its status; reports
    MCP progress while waiting. Replaces `run_status` polling loops.
22. [x] Profiling (item 4 of `docs/design/profiler.md` in `../ico`; needs
    apriori3d/ico#41–#46): `run(profile=true)` attaches ICO's
    `ProfilerTool`; the kernel streams `profile` notifications to the
    dashboard (columns on the plan), the result gets the five nodes with
    the largest self time (`profile.top`, formatted as on the dashboard),
    and the trace keeps every node in numbers. Guide: reading a profile.
23. [x] Tool calls below the client timeout (items 1 and 4 of the second
    addendum of the first domain session proposal): `timeouts.wait` (45 s)
    in `ico.toml`; `run(wait=true)` returns a still running run with
    `state: running` and `waited`, the run goes on in the background;
    `wait_run` cuts its `timeout` to `timeouts.wait`. Runs that survive a
    server restart are in Deferred.
24. [x] Crystallize below the client timeout: `crystallize` waits at most
    `timeouts.wait`, then returns `state: running` with a `crystallize_id`
    and its `stage`; the work goes on in the server and
    `wait_crystallize(crystallize_id)` returns its outcome. The drafts
    lock is held only until the drafts are copied into the worktree.
25. [x] Cancel between items (needs apriori3d/ico#53): `cancel_run` sends
    SIGTERM to the run's kernel, which cancels the run's `IcoCancelToken`;
    the run stops between items and returns the outputs so far (with its
    trace and profile) and `kernel_error` "cancelled"; a run that has not
    stopped within `CANCEL_GRACE` (10 s) is killed as before.
26. [x] Samples (C.2 of the core evolution proposal in `../ico`; needs
    apriori3d/ico#55): `sample_run(run_id, node, count)` returns previews
    of the next results of a node of a running run (plan key or label);
    the run's kernel handles one-way messages on a stdin thread and
    answers with notifications of the run.
27. [x] Server as a package, domain as a repository of its own (replaces
    the template of item 2): `ico-mcp init <dir> [--package]` creates a
    domain repository (ico.toml, pyproject depending on ico-mcp and ico,
    packages, pipelines, examples, the examples test from
    `ico_mcp.testing`, `docs/domain.md`, `.mcp.json`); drafts are
    committed there and `crystallize` moves them out of the draft package;
    the server finds the root by `--root` or the nearest `ico.toml`;
    `about` in `ico.toml` goes into the instructions and `guide` serves
    the package guide plus `docs/domain.md`. First domain: `ico-mcp-video`
    on the cube machine.

Findings of the first domain (numbers of
`docs/proposals/2026-10-17-first-domain-session.md`, which now continues in
the `ico-mcp-video` repository; this copy is frozen). ICO items are in the
`ico` roadmap (17-21). One PR each:

28. [x] Small fixes for remote use: a fixed `board = { host, port }` in
    `ico.toml` (finding 5: the dashboard moves to a new port on every
    restart, so a LAN proxy breaks); `environment(match=[...])` as a list
    (finding 6); a JSON-quoted string `input` is decoded or rejected with
    a hint (finding 3); the profile says "first call" instead of
    `startup` when it is not setup (finding 8).
29. [x] `delete_module(module)` (finding 24): removes a draft module and
    reports the saved pipelines that use its operators, like `changes`.
30. [x] `sample_run`, second iteration (finding 21): the last N results of
    a node (a ring buffer, so an agent sees items that already passed),
    and a field-wise preview of dataclasses (scalars in full, arrays as
    shape and dtype) instead of a repr cut in the middle.
31. [x] `pipeline_ref` on the server side (finding 16, with ico item 21):
    `save_pipeline` records the referenced pipelines; replacing or
    deleting one reports the pipelines that use it.

32. [x] Remote runs (ico `docs/design/remote.md`): targets in `ico.toml`,
    `run(target=)` in a kernel that loads no operators and bridges the
    job's events, code by value (drafts and stable, with their imports,
    and the assets manifest), upload / download of local files, keys only
    for remote kernels.
35. [x] Assets on a target: the runner fails fast with `missing_assets`
    (ico), `fetch_assets(target=)` sends a prefetch job.
36. [x] Findings of the resources migration: the run result's `profile`
    and the trace carry the resource rows (main and worker processes;
    needs the ico change); `reload` reads `ico.toml` again and lists what
    applies now and what needs a reconnect.
37. [x] Network volumes per order (needs apriori3d/ico#86): `volume_up` /
    `volume_down` (confirm), the volume of a target in
    `runs/targets/<name>.json`, a target without `storage` uses it as its
    storage (`no_volume` otherwise), runs carry `Job.volume`.

Findings of the first runs on RunPod (`animate_loop` on a 48 GB GPU). One
PR each:

38. [x] The plan of a remote run at once: the main kernel builds it from
    the spec when the run starts, so the dashboard shows the plan (with
    its progress nodes at zero) while the job waits for a GPU; today it
    comes with the worker's first events, and a 30-minute wait for a GPU
    showed an empty card. The same for the prefetch job of `fetch_assets`.
39. [ ] `assets_status` / `fetch_assets` of a saved pipeline without
    `params` take the pipeline's `check_params`; today a required
    parameter (`out_dir`) fails them.
40. [x] A target with `storage` and an order volume stages its inputs and
    outputs in the storage, never through the volume's S3 API (its keys
    differ from `AWS_*`, and its datacenter's API went down); the volume
    keeps assets, work dirs and caches.
41. [x] Job directories (needs the ico change of `Job.cleanup`, `$RUN`):
    a remote run's `ico-jobs/<run_id>` on the volume is removed when the
    run ends, whatever the outcome (`cleanup = true` per target by
    default, `run(cleanup=false)` keeps one); `$RUN` in input / params
    strings and `ICO_RUN_ID` give operators the run id, locally and on
    the worker.
42. [x] Results of remote runs without a live stream (ico
    `docs/proposals/2026-10-26-remote-results.md`; needs apriori3d/ico#111
    and #112): `runs/<run_id>/remote.json` at submit (target, RunPod id,
    storage URIs), the job's `status` URI `runs/<run_id>/result.json`,
    `run_id` and `target` in traces, `remote_error` with the ids when the
    stream is lost past `ICO_REMOTE_RETRY`, and the `fetch_run` tool.

Assets and resources (ico `docs/proposals/2026-10-20-assets-and-resources.md`,
its item 6; needs apriori3d/ico#75-#78):

33. [x] Kernel env (`[assets] root`, `[resources] ram / gpu`, the
    manifest beside `ico.toml`), `validate` warnings (`missing_asset`,
    `unknown_asset`, `no_assets_manifest`), assets in `get_operator` and
    `environment`, the guide.
34. [x] `assets_status` / `fetch_assets` as jobs (ask the human above a
    few GB), instructions, crystallize gate (asset ids in the manifest and
    lock, assets with sizes in the PR).

## Deferred
See "Deferred" in `docs/design/ico-mcp.md`. Do not implement without a
real need found in actual use.
- Dashboard as an MCP App: the same page as a `ui://ico/run` resource
  linked from `run` via `_meta.ui.resourceUri`, adapter via
  `callServerTool`. Revisit when MCP Apps render reliably in the desktop.
