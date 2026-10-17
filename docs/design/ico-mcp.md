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
- **This repository is the template.** It contains the domain-agnostic
  server and kernel plus empty domain packages. A domain MCP is a clone of
  it: operators, pipelines and domain dependencies live in the clone, and
  crystallization PRs go to the clone's own repository (may be private).
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

The repository root is the domain root; cloning is the initialization.

```
ico-mcp/                     # clone → e.g. mydomain-mcp
  src/ico_mcp/               # server and kernel (domain-agnostic)
  domain/                    # stable operators and types (package)
    __init__.py
  domain_draft/              # drafts written by the agent (package)
    __init__.py
  pipelines/<name>.spec.json # saved, validated specs
  examples/<op>.json         # examples for JSON-io operators (<name>@<version>)
  runs/YYYY-MM-DD.jsonl      # traces (gitignored)
  ico.toml
  pyproject.toml             # a clone adds its domain dependencies here
```

`ico.toml` (minimal):

```toml
name = "domain"
package = "domain"             # stable package; drafts in domain_draft
github = { repo = "owner/domain-mcp", base = "main" }
timeouts = { run = 600, validate = 30, load = 60 }
env = ["HF_HOME"]              # variables passed to the kernel
typecheck = "strict"           # pyright mode for drafts
```

One environment: the kernel is a separate process but runs with the same
interpreter (`sys.executable`) and `.venv` as the server, so domain
dependencies (e.g. torch, model libraries) are added to the clone's
`pyproject.toml`.

**Specializing a clone:** rename `domain` / `domain_draft` and set `name`,
`package` and `github` in `ico.toml`; the README documents these steps.

**Trade-off:** server fixes do not reach clones automatically; a clone
pulls them from the upstream template with git. Fine for one or two
domains. With a second or third domain, the server moves into a package and
the template into `copier` (see "Deferred").

**Status comes from location.** Operator source does not set `status`.
Modules in `<pkg>_draft` load as `draft`, modules in `<pkg>` as `stable`.
This needs a small core change: `load_operators(path, status=...)` override
(see roadmap item 0). `verified` is not used in the MVP.

**Type names.** Agents refer to types by short name (`Report`). The kernel
resolves a short name over all types used in registered signatures; an
ambiguous name is an error listing the candidates as `module:qualname`.

## Tools (MVP)

| Tool | Params | Returns |
|---|---|---|
| `search_operators` | `text?`, `input_type?`, `output_type?`, `tags?`, `limit?` | operator cards |
| `get_operator` | `ref` | card + source code (for editing only) |
| `propose_operator` | `module`, `code`, `typecheck?=true`, `examples?` | cards or a staged error report |
| `validate` | `spec` | issues with spec paths |
| `run` | `spec`, `input?`, `max_items?` | `RunResult.to_dict()`; writes a trace |
| `save_pipeline` | `name`, `spec`, `description` | saved path (must validate) |
| `list_pipelines` / `get_pipeline` | `name?` | pipelines / spec |
| `crystallize` | `refs`, `pipelines?`, `title`, `summary` | PR URL |
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

1. Check: refs are drafts; the working tree is clean; `gh` is installed
   and authenticated (otherwise an error with setup instructions).
2. Create branch `crystallize/<slug>` from `github.base`.
3. Move the modules of the refs (and the draft types they need) from
   `<pkg>_draft` to `<pkg>`; save the given pipelines; write tests from the
   examples.
4. Gate on the whole stable package: pyright strict, the generated tests,
   and `validate` of every saved pipeline.
5. Commit, push, `gh pr create`. The PR body contains: operator cards, the
   gate report, and trace statistics per operator (runs, successes, last
   run) from `runs/`.
6. Return the PR URL. The human reviews the abstraction (operator
   boundaries, types, params), not every line, and merges; then `reload`.

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
in the Code tab nor in a chat (tested with a 20 s run). So the dashboard
comes first and does not depend on the host:

- A small HTTP server in the server process, bound to `127.0.0.1`, with a
  one-time token in the URL, serves one HTML + JS page (scripts bundled).
- The server keeps the live state of active and recent runs from the
  kernel's progress notifications, for synchronous runs too. The page polls
  it: all progress nodes (label, done, total, rate, eta), state, errors and
  the final summary. A `dashboard` tool returns the URL; `run` results
  include it. The desktop app can show it in its built-in browser pane.
- All data access goes through a small adapter, so the same page can later
  be hosted as an MCP App (`ui://ico/run`, adapter via `callServerTool`).

Then asynchronous runs: `run(wait=false)` returns a `run_id`,
`run_status(run_id)` returns the same snapshot the dashboard uses,
`cancel_run` stops a run. This also avoids client tool-call timeouts on
long runs.

Later (deferred): the MCP App host and the pipeline graph from the ICO
`describe` JSON backend with progress overlaid on graph nodes.

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
- Server as a package and the template as `copier`, so server fixes reach
  clones by a version bump. Revisit with the second or third domain.
- Entry points for installed domain packages.
- Resources in specs, context nodes (see ico `docs/design/spec.md`).
