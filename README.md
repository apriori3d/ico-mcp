# ico-mcp

A universal MCP server on top of [ICO](https://github.com/apriori3d/ico), a framework for typed,
composable pipelines. An agent builds pipelines from a registry of typed
operators, runs them, writes the operators that are missing, and
*crystallizes* the ones that proved useful into stable operators through
a GitHub pull request that a human reviews.

```
search_operators → validate → run ─┬─ works ─→ save_pipeline → crystallize → PR → merge → reload
                                   └─ step missing → propose_operator / propose_types
```

Operators are not MCP tools: the tool set is fixed, and the agent sees
operators as compact cards found by type. Data crosses the JSON boundary by
reference (paths), and every run has a process of its own. The decisions
are in [docs/design/ico-mcp.md](docs/design/ico-mcp.md); the reference for
agents is [src/ico_mcp/agent-guide.md](src/ico_mcp/agent-guide.md).

**This repository is the server; a domain is a repository of its own.**
`ico-mcp init <dir>` creates one: its operators, types, pipelines, examples,
notes for agents and dependencies live there, its crystallization pull
requests go to its repository, and it depends on the `ico-mcp` package, so
server fixes reach it by a version bump (or at once, from an editable
checkout).

## Setup

Requirements:

- Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/);
- a checkout of ICO next to this repository (`../ico`): during development
  ICO is an editable path dependency (`[tool.uv.sources]` in
  `pyproject.toml`);
- [gh](https://cli.github.com), authenticated (`gh auth login`), only for
  `crystallize`.

```bash
uv sync
```

```bash
make ci
```

`make ci` runs ruff, pyright (strict) and the tests; the tests never touch
the network.

## Connect a client

The server speaks MCP over stdio. Use absolute paths: clients start it from
their own working directory.

The server runs in the domain's environment (`uv run --project`), so the
operators' kernels see the domain's dependencies.

**Claude Code**, from any directory:

```bash
claude mcp add video -- uv run --project /abs/path/to/ico-mcp-video ico-mcp --root /abs/path/to/ico-mcp-video
```

or the `.mcp.json` that `ico-mcp init` writes in the domain repository (it
is machine-specific and ignored by git):

```json
{
  "mcpServers": {
    "ico-mcp-video": {
      "command": "uv",
      "args": ["run", "--project", "/abs/path/to/ico-mcp-video",
               "ico-mcp", "--root", "/abs/path/to/ico-mcp-video"]
    }
  }
}
```

**Claude desktop app**: add the same `mcpServers` block to
`claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
`%APPDATA%\Claude\claude_desktop_config.json`) and restart the app. The
app does not inherit your shell's `PATH`, so set `"command"` to the
absolute path of `uv` (`which uv`). The Code tab also reads the project's
`.mcp.json`.

The server serves the domain repository given by `--root`, else the nearest
`ico.toml` from its working directory up.

## Workflow

1. **Reuse.** `list_pipelines` / `get_pipeline`, else compose from
   operators: `search_operators` by input and output type, `validate`,
   `run`.
2. **Extend.** When a step is missing, the agent writes a draft:
   `propose_operator` (stages write → typecheck → load → register →
   examples) and `propose_types` for shared types. Drafts live in
   `domain_draft/` (ignored by git) and load as `draft`.
3. **Run and watch.** `run` returns outputs and the URL of the dashboard
   (the ICO Live Board: progress of every node and the pipeline plan). Long
   runs start with `run(wait=false)` and are followed with `wait_run` or
   stopped with `cancel_run`; `sample_run` shows what flows out of a node
   while the run goes. `run(profile=true)` adds the time of every
   node to the plan on the dashboard and the slowest nodes to the result.
4. **Keep.** `save_pipeline` stores a validated spec in `pipelines/`.
5. **Crystallize.** With the human's consent, `crystallize` copies the
   drafts into the stable package in a branch of its own (the working tree
   is not touched), runs a gate (load, pyright strict, no draft imports,
   the examples, every saved pipeline validates), pushes and opens a pull
   request with the operator cards, the gate report and run statistics.
6. **Merge.** The human reviews the abstraction and merges, pulls, and the
   agent calls `reload`: drafts that are now stable are retired.

| Tool | Does |
|---|---|
| `search_operators` | find operators by input / output type, text, tags; compact cards |
| `get_operator` | a card with the operator's source |
| `list_types` / `get_type` | domain types with fields and docstrings |
| `validate` | type-check a spec; issues carry their location in the spec |
| `run` | run a spec on a JSON input; `wait=false` returns a run id at once; `profile=true` measures every node |
| `run_status` / `wait_run` / `cancel_run` | follow, wait for or stop a run |
| `sample_run` | previews of what flows out of a node of a running run |
| `dashboard` | the URL of the dashboard |
| `guide` | the reference for agents (`docs/agent-guide.md`) |
| `propose_operator` / `propose_types` | add or replace a draft module, in checked stages |
| `delete_module` | remove a draft module; reports the saved pipelines that used it |
| `save_pipeline` / `list_pipelines` / `get_pipeline` / `delete_pipeline` | saved, validated specs |
| `crystallize` / `wait_crystallize` | a pull request that makes drafts stable; a long gate goes on in the background |
| `reload` | restart the kernel after a merge or manual edits |
| `environment` | Python, PATH, packages (and duplicate builds), binaries of `bin:` caps, assets and resource budgets, as operators see them |
| `assets_status` / `fetch_assets` | the state of the assets (`assets.toml`) a pipeline needs; download them as a run, above 2 GB only with `confirm=true` (the human's word) |
| `server_status` | the code this server process runs; `stale` when the code on disk is newer (reconnect the server) |

## Create a domain

```bash
uv run --directory /abs/path/to/ico-mcp ico-mcp init /abs/path/to/ico-mcp-video --package video
```

creates, without touching existing files:

```
ico.toml              name, package, about, github, timeouts, env, typecheck, board
pyproject.toml        depends on ico-mcp and ico (editable checkouts when the
                      server runs from one, else their git repositories)
video/ video_draft/   stable operators and types; drafts (committed too)
pipelines/ examples/  saved pipelines; examples of pure operators
tests/test_domain_examples.py   the examples as tests (crystallize gate)
docs/domain.md        the domain's part of the guide
.gitignore .mcp.json
```

Then:

1. Add the domain's dependencies to `pyproject.toml` (e.g. torch) and run
   `uv sync`; operators run in this environment.
2. In `ico.toml`: `about` (a few lines; agents see them in the server
   instructions), `github = { repo = "owner/ico-mcp-video", base = "main" }`
   for crystallization, `env` for the variables operators may read (e.g.
   `HF_HOME`), and `timeouts`. `timeouts.wait` (45 s) is the longest a tool
   call waits for a run or a crystallization: keep it below your client's
   tool-call timeout (about 60 s in the Claude app), which restarts the
   server and loses its runs. `board = { host = "0.0.0.0", port = 8765 }`
   keeps the dashboard on one port across restarts and serves it to the
   local network (default: `127.0.0.1`, a free port); its URL carries a
   token.
3. Write `docs/domain.md`: the workflow (which pipeline comes after which),
   how to choose between alternatives, data locations and formats, naming
   rules, hardware. The `guide` tool serves it after the general guide.
4. `git init`, commit, and push to the domain's repository.

**Moving a domain out of a clone of this repository:** create the domain
with `init`, copy `domain_draft/` (into `<package>_draft/` with the
package renamed in imports), `domain/`, `pipelines/`, `examples/`, the
`ico.toml` values and the dependencies, commit, then reset the clone to a
clean `main`.

## Development

```
src/ico_mcp/
  server.py       MCP tools and instructions; hosts the dashboard
  kernel.py       child process: imports operators, search, validate, run
  kernel_client.py  starts, times out and restarts kernels
  jobs.py         runs, each in a kernel of its own
  proposals.py    propose_operator / propose_types stages
  drafts.py       relative imports, closure, retiring crystallized drafts
  crystallize.py  worktree, gate, pull request
  scaffold.py     ico-mcp init: an empty domain repository
  testing.py      the examples of stable operators as tests, for domains
  agent-guide.md  the guide for agents (the domain adds docs/domain.md)
  domain_types.py, type_names.py, pipelines.py, traces.py, progress.py, profiles.py
```

The server process never imports operator code; only kernels do. Tests use
local bare git repositories as remotes and a fake `gh` executable.

CI (`.github/workflows/ci.yml`) runs `make ci` on Python 3.11 and 3.12 for
pushes to `main` and pull requests, with `apriori3d/ico` checked out next
to this repository from its `main`.
