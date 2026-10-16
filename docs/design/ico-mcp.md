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
- The server is domain-agnostic. Domain work lives in a **workspace**
  (a separate git repository, may be private). Later the workspace layout
  becomes the domain template.
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
  transport only), workspace files, pyright, git and `gh`, run traces. It
  never imports operator code.
- **Kernel** (child process, restartable): imports the workspace operator
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

## Workspace

```
<workspace>/                  # git repo with a GitHub remote
  ico.toml
  <pkg>/                      # stable operators and types (package)
    __init__.py
  <pkg>_draft/                # drafts written by the agent (package)
    __init__.py
  pipelines/<name>.spec.json  # saved, validated specs
  examples/<op>.json          # examples for JSON-io operators
  runs/YYYY-MM-DD.jsonl       # traces (gitignored)
```

`ico.toml` (minimal):

```toml
name = "mydomain"
package = "mydomain"           # stable package; drafts in mydomain_draft
python = ".venv/bin/python"    # interpreter for the kernel (workspace deps)
github = { repo = "owner/mydomain", base = "main" }
timeouts = { run = 600, validate = 30, load = 60 }
env = ["HF_HOME"]              # variables passed to the kernel
typecheck = "strict"           # pyright mode for drafts
```

The kernel runs with the workspace's own interpreter, so domain dependencies
(e.g. torch, model libraries) belong to the workspace, not to ico-mcp.

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
   only, using the workspace interpreter. Pyright's node part is
   pre-fetched at install/image build time.
3. **load**: restart the kernel; load errors of that module are reported.
4. **register**: the module registers at least one operator; no signature
   is inferred as `Any`.
5. **examples**: for JSON-io operators, each `{"input", "output"}` example
   must pass. Operators with domain types (e.g. `Image`) are exercised
   through pipeline runs instead.

On failure the file stays in the draft package so the agent can read and fix
it, but its entries are not loaded.

### `crystallize`

Prepares a human review as a GitHub pull request:

1. Check: refs are drafts; the workspace tree is clean; `gh` is installed
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
paths, duration, refs used. Traces feed `crystallize`. No automatic status
promotion in the MVP.

### Progress

The console progress rendering of ICO (`RichProgressTool`) is replaced by
MCP progress notifications. When the client sends a progress token with a
`run` request, the kernel attaches `JsonProgressTool` (ICO core): a runtime
tool that resolves user-friendly labels from the runtime tree and emits
throttled JSON snapshots. The kernel streams them as protocol
notifications; the server forwards them as MCP progress notifications. One
notification carries one `progress` / `total` / `message`, so the
outermost progress node gives `progress` and `total`, and other nodes are
summarized in `message`, e.g. "inpaint 120/900 · detect 300/900".

MCP constraints (spec, "Progress"): one progress token per request, so one
stream per `run`; no nesting; `progress` must increase with every
notification; `total` is optional; rate limiting is expected. Nested
progress is therefore flattened into one monotonic scalar: for an outer
node with an inner node that restarts (e.g. epochs × batches),
`progress = outer_done + inner_done / inner_total`, `total = outer_total`.
Per-node detail lives only in `message` and in the run trace.

These notifications are mainly for the human in the client UI; the model
usually does not read them. Client support varies and must be checked
with Claude Code and the desktop app.

### Asynchronous runs and dashboard

`run(wait=false)` returns a `run_id`; `run_status(run_id)` returns a compact
snapshot of all progress nodes; `cancel_run` stops a run. This also avoids
client tool-call timeouts on long runs.

The dashboard is an MCP App (rendered by Claude Desktop and claude.ai; Claude
Code stays text-only). A `ui://ico/run` resource is linked from `run` and
`describe`. The page:

- renders the pipeline graph from the `describe` JSON view model produced
  by ICO core; the page knows no node kinds, so a new node type needs no
  JS change;
- polls `run_status` through `callServerTool` and overlays progress on
  graph nodes by `progress_key`; nested progress is shown in full here,
  unlike MCP progress notifications;
- shows the result summary and output paths when the run ends.

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
- Workspace template (`copier`), entry points for installed domain packages.
- Resources in specs, context nodes (see ico `docs/design/spec.md`).
