# ico-mcp: design

## Goal

A universal MCP server on top of ICO. An agent builds pipelines from a
registry of typed operators, runs them, and writes new operators when one is
missing. Repeated work is *crystallized*: drafts that proved useful are
proposed to a human as a GitHub pull request and become `stable`.

Not goals: a hosted service for clients, a UI, a general code sandbox.

Key principles (decided):

- Operators are **not** MCP tools. The tool set is fixed; the agent sees
  operators only as compact cards returned by search.
- **This repository is the server; a domain is a repository of its own.**
  The server and kernel are domain-agnostic and ship as the `ico-mcp`
  package. A domain repository (`ico-mcp init`) holds operators, types,
  pipelines, examples, notes for agents and the domain's dependencies, and
  depends on `ico-mcp`; crystallization PRs go to its repository (may be
  private). Server fixes reach a domain by a version bump.
- JSON boundary, data by reference: inputs and outputs are JSON; images,
  videos and other large data are passed as paths or URIs, never as bytes
  through the agent's context. `run` is `ico.spec.run` from core.
- YAGNI: every feature below is needed by the crystallization loop.
  Everything else is in "Deferred".

## Architecture

Two processes:

```
agent ── stdio (MCP) ──► server ── JSON lines over pipes ──► kernel
                         │                                   │
                         files, pyright, git, gh, traces      registry, build,
                                                              validate, run
```

- **Server** (long-lived): MCP protocol (official Python MCP SDK, stdio
  transport only), repository files, pyright, git and `gh`, run traces. It
  never imports operator code.
- **Kernel** (child process, restartable): imports the domain operator
  packages with `ico.registry.load_operators`, holds the registry and
  executes `search`, `card`, `validate`, `run`.

Why a separate kernel:

- replacing a draft = restarting the kernel; no hot reload of imported
  modules is needed;
- a crash or a hang of agent code does not take down the server; timeouts
  are enforced by killing the kernel;
- it is the place where container isolation will go later.

Protocol: one JSON object per line, request `{"id", "method", "params"}`,
response `{"id", "result"}` or `{"id", "error": {...}}`. Errors are
structured (`SpecError` fields: `reason`, `path`, `expected`, `got`,
`refs`). Each request has a timeout from `ico.toml`; on timeout the server
kills and restarts the kernel and returns a `timeout` error.

The kernel's environment is an allowlist: secrets in the server's
environment are not passed to it unless listed in `ico.toml`.

## Repository layout

The server is this repository (the `ico-mcp` package); a domain is a
repository of its own, created by `ico-mcp init <dir> [--package <pkg>]`
(existing files are kept):

```
ico-mcp-video/               # a domain repository
  video/                     # stable operators and types (package)
    __init__.py
  video_draft/               # drafts written by the agent (package, committed)
    __init__.py
  pipelines/<name>.spec.json # saved, validated specs
  examples/<op>.json         # examples for JSON-io operators (<name>@<version>)
  tests/test_domain_examples.py  # one line: ico_mcp.testing.example_tests
  docs/domain.md             # the domain's part of the guide
  runs/YYYY-MM-DD.jsonl      # traces (gitignored)
  ico.toml
  pyproject.toml             # ico-mcp, ico and the domain's dependencies
  .mcp.json                  # uv run --project <root> ico-mcp --root <root>
```

`ico.toml` (minimal):

```toml
name = "ico-mcp-video"
package = "video"              # stable package; drafts in video_draft
about = "Removes burned-in subtitles from marketplace videos."
github = { repo = "owner/ico-mcp-video", base = "main" }
timeouts = { run = 600, validate = 30, load = 60, wait = 45 }
env = ["HF_HOME"]              # variables passed to the kernel
typecheck = "strict"           # pyright mode for drafts
```

The server finds the domain by `--root`, else by the nearest `ico.toml`
from its working directory up.

One environment per domain: the server runs in the domain's environment
(`uv run --project <domain>`), and the kernel is a separate process with
the same interpreter (`sys.executable`), so the operators see the domain's
dependencies (e.g. torch, model libraries). `pyproject.toml` takes
`ico-mcp` and `ico` from editable checkouts when the server that ran
`init` runs from one (development), else from their git repositories. One
server process serves one domain: domains differ in environments and in
the repositories their pull requests go to.

Knowledge for agents comes in three layers: the server instructions (with
the domain's `about`), the `description` of each saved pipeline (what it
does, its input, which pipeline comes before and after it), and
`docs/domain.md`, served by `guide` after the general guide (shipped with
the package): the workflow across pipelines, how to choose between
alternatives, data locations, conventions, hardware.

Drafts are committed in the domain repository, so work in progress is
versioned; `crystallize` moves a draft module to the stable package in its
branch, and the merge removes it from the draft package.

**Status comes from location.** Operator source does not set `status`.
Modules in `<pkg>_draft` load as `draft`, modules in `<pkg>` as `stable`.
This needs a small core change: `load_operators(path, status=...)` override
(see roadmap item 0). `verified` is not used in the MVP.

**Libraries.** A domain can use the operators and types of installed
packages, e.g. Apriori Studio's `layers` and `marketplace` (editable from
their checkout during development):

```toml
libraries = ["layers", "marketplace"]
```

- A library is a package installed in the domain's environment
  (`pyproject.toml`). The kernel loads its top-level modules as `stable`,
  **before** the domain's packages: a draft that imports a library module
  would otherwise register its operators with their own status. A run's
  kernel loads only the library modules its spec uses, as for the domain.
- Its operators and types are found by `search_operators`, `get_operator`,
  `list_types` and `get_type` like the domain's, and drafts import them
  absolutely (`from layers.masks import Mask`).
- The server never edits or crystallizes a library: it changes through
  pull requests to its own repository. `reload` restarts the kernel, so
  edits in an editable checkout apply without a reconnect.
- A library that is not installed is an error of the load report
  (`libraries.<name>.errors`); the domain's packages load all the same.
- Remote runs do not carry libraries by value: a target's image has to
  install them (not supported by the runner yet; no domain needs it).
- A name equal to the domain's package or its draft package, or listed
  twice, is a configuration error.

**Type names.** Agents refer to types by short name (`Report`). The kernel
resolves a short name over all types used in registered signatures; an
ambiguous name is an error listing the candidates as `module:qualname`.

## Domain types

A data type is a plain Python class used in operator signatures:
dataclasses, Pydantic models, or library types (`np.ndarray`,
`PIL.Image.Image`, `pandas.DataFrame`, builtins). There is no separate type
system and no standard type library yet; the vocabulary grows from real
domains through crystallization, and common types may be lifted into a
shared package later.

Shared vocabulary is what keeps operators composable, so the agent must
find and reuse types before creating new ones:

- `propose_types(module, code)` adds a types-only module to the draft
  package (stages write → typecheck → load). Operator modules import
  their types from it.
- `list_types` / `get_type(name)` show the domain's types with module,
  status, docstring and fields, including types no signature uses yet.
- Types move to the stable package with the operators that use them on
  `crystallize`.

**Imports.** `module` in `propose_types` / `propose_operator` is a module
name inside the draft package; the agent never chooses the package. To keep
crystallization a plain move of files, draft → draft imports are relative
(`from .frames import Frame`) and draft → stable imports are absolute
(`from domain.frames import Frame`). Absolute imports of the draft package
are rejected at the `write` stage. The stable package never imports the
draft package; the `crystallize` gate checks this, so reviewed code never
depends on unreviewed code. Moved modules keep their relative imports
because an operator and the types it uses move together.

Two edge cases of a partial crystallization:

- A moved module relatively imports a draft module that was not selected:
  `crystallize` adds such modules automatically (the closure of relative
  imports) and lists them in the PR.
- A draft module that stays relatively imports a module that moves: its
  import is rewritten to the absolute stable form (`from .frames import`
  → `from domain.frames import`). Only remaining drafts are rewritten;
  moved, reviewed code is never touched. The kernel reload after the move
  checks that the drafts still load.

Guidelines in the server `instructions`: types are immutable dataclasses
or Pydantic models with a docstring; large data crosses the pipeline
boundary by reference (e.g. `ImageRef` with a path), while data inside a
pipeline can stay in memory (e.g. a `Frame` with a pixel array).

## Tools (MVP)

| Tool | Params | Returns |
|---|---|---|
| `search_operators` | `text?`, `input_type?`, `output_type?`, `tags?`, `limit?` | operator cards |
| `get_operator` | `ref` | card + source code (for editing only) |
| `propose_operator` | `module`, `code`, `typecheck?=true`, `examples?` | cards or a staged error report |
| `propose_types` | `module`, `code`, `typecheck?=true` | type cards or a staged error report |
| `list_types` / `get_type` | `name?` | type cards (module, status, docstring, fields) |
| `validate` | `spec` | issues with spec paths |
| `run` | `spec`, `input?`, `max_items?` | `RunResult.to_dict()`; writes a trace |
| `save_pipeline` | `name`, `spec`, `description` | saved path (must validate) |
| `list_pipelines` / `get_pipeline` | `name?` | pipelines / spec |
| `crystallize` | `refs`, `pipelines?`, `title`, `summary` | PR URL, or `crystallize_id` when still running |
| `wait_crystallize` | `crystallize_id`, `timeout?` | as `crystallize` |
| `reload` | — | load report (after a merge or manual edits) |

### `propose_operator`

Stages; the first failing stage stops and is reported as
`{"stage": ..., "diagnostics": [...]}` with file, line, rule and message
where applicable:

1. **write**: validate the module name, write `<pkg>_draft/<module>.py`.
   Replacing an existing draft module is allowed; a module name that exists
   in the stable package is rejected.
2. **typecheck** (default on): pyright in the configured mode on that file
   only, using the repository environment, with the `validate` timeout.
   Errors fail the stage; warnings are returned with the result. Pyright
   is a runtime dependency; its node part is pre-fetched at install/image
   build time.
3. **load**: restart the kernel; load errors of that module are reported.
4. **register**: the module registers at least one operator; no signature
   is inferred as `Any`.
5. **examples**: for JSON-io operators, each `{"ref", "input", "output"}`
   example must pass: running `ref` on `input` gives exactly one output
   equal to `output`. Passed examples are saved to
   `examples/<name>@<version>.json` (replaced by the next proposal with
   examples for that ref); `crystallize` turns them into tests. Operators
   with domain types (e.g. `Image`) or iterator outputs are exercised
   through pipeline runs instead.

On failure the file stays in the draft package so the agent can read and
fix it with another proposal. Nothing is rolled back: whatever of the file
loads stays loaded, and the entries of a replaced version are gone until
the module is fixed.

### `crystallize`

Prepares a human review as a GitHub pull request:

1. Check: `github` is set; refs are drafts; the pipelines are saved; `gh`
   is installed and authenticated (otherwise an error with setup
   instructions).
2. Create branch `crystallize/<slug>` from `origin/<github.base>` in a
   temporary git worktree. The working tree, from which the server runs,
   is never switched or changed, so it need not be clean (saved examples
   and pipelines are usually uncommitted there).
3. Copy the modules of the refs (and the closure of their relative
   imports) from `<pkg>_draft` to `<pkg>`, unchanged, with the examples of
   their operators and the given pipelines; the moved drafts leave the draft
   package of the branch. Examples are the tests: a domain has one test,
   `tests/test_domain_examples.py` (from `init`), that runs the examples of
   every stable operator.
4. Gate on the whole stable package: it loads, pyright strict, no import of
   the draft package, the examples pass, and `validate` of every saved
   pipeline. A failing gate pushes nothing and removes the branch.
5. Commit, push, `gh pr create`. The PR body contains: operator cards, the
   gate report, and trace statistics per operator (runs, successes, last
   run) from `runs/`.
6. Return the PR URL. The human reviews the abstraction (operator
   boundaries, types, params), not every line, and merges; then `reload`.

The gate can take longer than a client's tool-call timeout, so, as for runs,
no call waits longer than `timeouts.wait`: a crystallization still going
then returns `state: running`, its `crystallize_id` and `stage`, and goes
on in the server; `wait_crystallize(crystallize_id)` waits for it (at most
`timeouts.wait` per call) and returns what `crystallize` would have. The
drafts lock (shared with proposals) is held only through step 3; the gate
and the push work on the worktree. Recent outcomes are kept in memory: a
server restart loses an active crystallization (its worktree is removed
when the server stops normally), as it loses runs.

Drafts stay in the draft package until the merge, so the agent keeps using
them while the PR is reviewed. After the human pulls the merge, `reload`
(and server start) retires every draft whose stable module of the same name
is identical: the draft is removed and the remaining drafts import the
stable module. A draft that was changed after crystallization is kept and
reported as a conflict (it does not load, its operators are registered by
the stable module).

### Traces

Every `run` appends one line to `runs/YYYY-MM-DD.jsonl`: timestamp, spec,
input, summary of the result (counts, truncated outputs), errors with spec
paths, duration, refs used, final progress of each node. Traces feed `crystallize`. No automatic status
promotion in the MVP.

### Progress

The console progress rendering of ICO (`RichProgressTool`) is replaced by
MCP progress notifications. The kernel attaches `JsonProgressTool` (ICO
core) to every `run`: a runtime tool that resolves user-friendly labels from
the runtime tree and emits throttled JSON snapshots, and on activation
reports every progress node once with `done = 0`, in flow order. The kernel
streams the snapshots as protocol notifications.

MCP constraints (spec, "Progress"): one progress token per request, so one
stream per `run`; no nesting; `progress` must increase with every
notification; `total` is optional; rate limiting is expected. The status is
therefore the **first progress node** of the flow (the first snapshot):
`progress` / `total` are its `done` / `total`, `message` is
`"<label> <done>/<total>"`. A notification that would not increase
`progress` (the node restarts in a loop) is skipped. Runtime keys are flat
paths, so nesting is not inferred; the order of nodes in the flow decides.
Other nodes are not reported as MCP progress: their final state goes into
the run trace, and the dashboard shows them all.

The server always forwards the status to the SDK, which sends nothing when
the client did not send a progress token.

These notifications are mainly for the human in the client UI; the model
usually does not read them. Client support varies and must be checked
with Claude Code and the desktop app.

### Dashboard and asynchronous runs

MCP progress notifications were not shown by the Claude desktop app, neither
in the Code tab nor in a chat (tested with a 20 s run). So progress is shown
on a browser dashboard that does not depend on the host.

The dashboard is the ICO Live Board from ICO core: a page with console-like
progress bars on top and the pipeline graph from the `describe` JSON
backend below, served by a small stdlib HTTP server bound to `127.0.0.1`
with a one-time token. ico-mcp only hosts it:

- in the server process, not in the kernel, so the URL and run history
  survive kernel restarts;
- with the server's registry of active and recent runs as the data source,
  fed by the kernel's progress notifications (synchronous runs too), plus
  the `describe` JSON of each run's spec;
- a `dashboard` tool returns the URL; `run` results include it. The desktop
  app can show it in its built-in browser pane.

Then asynchronous runs: `run(wait=false)` returns a `run_id`,
`run_status(run_id)` returns the same snapshot the dashboard uses,
`cancel_run` stops a run. No tool call waits for a run longer than
`timeouts.wait` (45 s): a client restarts the server after its tool-call
timeout (about 60 s in the Claude app), and the server's runs die with it
(see "Runs that survive a server restart" in Deferred). `run(wait=true)`
returns a still running run with `state: running` and `waited`, and
`wait_run` cuts its `timeout` to `timeouts.wait`.

Each run (also with `wait=true`) has a kernel process of its own.
`cancel_run` sends SIGTERM to the run's kernel: ICO stops the run between
items (`ico.spec.run(cancel=...)`), the workers stop cleanly, and the
result keeps the outputs produced so far, the trace and the profile; a run
that has not stopped within `CANCEL_GRACE` (10 s) is killed with its
worker processes. The main kernel stays free for search, validate and
proposals during a run, and a reload does not cut runs short (they finish
on the code they loaded). The cost is importing the domain packages once
per run. Examples of `propose_operator` still run in the main kernel.

`sample_run` asks a running run for previews of a node's next results
(`IcoRuntime.sample` in ICO). The run's kernel reads stdin on a thread of
its own: lines without an `id` are one-way messages, handled at once while
a request runs, and answered with notifications of the run
(`sample_started` with the resolved plan key, or `sample_error`, then the
`sample` notifications). The server matches them by a token and waits at
most `timeouts.wait`. Labels resolve in the kernel, where the flow is.

Later (deferred): the same page as an MCP App (`ui://ico/run`).

### Server instructions

The MCP `instructions` field tells the agent the workflow, because the
token and time savings depend on it:

1. Look for a saved pipeline first (`list_pipelines`).
2. Otherwise compose from existing operators: `search_operators` step by
   step, `validate`, `run`.
3. Write a new operator (`propose_operator`) only when a step is missing.
   Keep operators small, typed, parametric; configuration goes into
   constructor params, not into closures; heavy models load lazily.
4. Pass data by reference (paths), never as bytes.
5. When a pipeline works and is likely to be reused, `save_pipeline`, then
   suggest `crystallize` to the human.

## Security (MVP)

- Agent code runs in the kernel process with the user's permissions.
  The kernel isolates *failures* (crashes, hangs), not *access*.
- `caps` are recorded on cards and shown in PRs but **not enforced** in the
  MVP. Container isolation with enforced caps is deferred.
- Secrets reach the kernel only through the `env` allowlist.
- Intended use: local, single user, trusted agent.

## Deferred

- Run progress as a subscribable MCP resource.
- Caching by content hash; artifact handles.
- Automatic `verified` status from traces.
- Domain tools published from saved pipelines.
- Container isolation and enforced `caps`.
- HTTP transport, multiple users.
- Publishing `ico-mcp` and `ico` to a package index (today: editable
  checkouts or git dependencies).
- Entry points for installed domain packages.
- Resources in specs, context nodes (see ico `docs/design/spec.md`).
- Runs that survive a server restart. Today a run lives in the server
  process: its kernel is the server's child, connected by pipes, and the
  registry of runs is in memory. When the server stops (the client
  restarts it after a tool-call timeout, a reconnect after `stale`, a
  crash), its active runs are killed or lost, and `run_status` of the new
  server answers `not_found`. A fix would detach the run's kernel from the
  server, write its state, progress and result to `runs/<run_id>/`, and
  let a new server pick up unfinished runs (`run_status`, `wait_run` and
  the dashboard by `run_id`). Not needed while tool calls stay below the
  client's timeout (`timeouts.wait`); revisit when restarts for other
  reasons cost real runs.
