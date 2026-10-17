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
9. [ ] Browser dashboard (first; MCP progress notifications were not shown
   by the desktop app, neither in the Code tab nor in a chat). One HTML + JS
   page served by a small HTTP server in the server process, bound to
   `127.0.0.1` only, one-time token in the URL. The server keeps the live
   state of active and recent runs from the kernel's progress
   notifications (also for synchronous `run`), and the page polls it:
   all progress nodes (label, done, total, rate, eta), state, errors,
   final summary. A `dashboard` tool returns the URL, and `run` results
   include it. Scripts bundled into the HTML (no CDN). All data access goes
   through a small adapter (`getStatus`) so the page can later be hosted as
   an MCP App. In the desktop app it can open in the built-in browser pane.
   The pipeline graph (`describe` JSON backend) is not part of this item.
10. [ ] Asynchronous runs: `run(wait=false)` returns a `run_id`;
   `run_status(run_id)` returns the same compact snapshot the dashboard
   uses; `cancel_run(run_id)`. Runs live in the kernel; the server tracks
   them.
11. [ ] `crystallize`: branch, move drafts to stable, tests from examples,
   gate (pyright strict, tests, validate pipelines, and no import of the
    draft package from the stable package); moved modules are never
    rewritten: the closure of their relative imports moves with them, and
    remaining drafts that import a moved module get that import rewritten
    to the absolute stable form, commit, push,
   `gh pr create` with cards, gate report and trace stats.
12. [ ] Docs: how to connect the server to Claude Code (`.mcp.json`) and to
   the desktop app; README with the workflow; how to
    specialize a clone (rename `domain` / `domain_draft`, set `ico.toml`).

## Deferred
See "Deferred" in `docs/design/ico-mcp.md`. Do not implement without a
real need found in actual use.
- Dashboard as an MCP App: the same page as a `ui://ico/run` resource
  linked from `run` via `_meta.ui.resourceUri`, adapter via
  `callServerTool`. Revisit when MCP Apps render reliably in the desktop.
- Pipeline graph in the dashboard: ICO core JSON backend for `describe`
  (deferred in `../ico`), nodes overlaid with progress by `progress_key`.
