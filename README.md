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
agents is [docs/agent-guide.md](docs/agent-guide.md).

**This repository is a template.** A domain server is a clone of it: its
operators, types, pipelines and dependencies live in the clone, and its
crystallization pull requests go to the clone's repository.

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

**Claude Code**, from any directory:

```bash
claude mcp add ico -- uv run --directory /abs/path/to/ico-mcp ico-mcp
```

or a project-scoped `.mcp.json` in the root of the project you work in
(it is machine-specific, so this repository ignores its own):

```json
{
  "mcpServers": {
    "ico": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/ico-mcp", "ico-mcp"]
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

`ico-mcp --root <dir>` serves another repository than the one the package
was installed from.

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
   stopped with `cancel_run`. `run(profile=true)` adds the time of every
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
| `dashboard` | the URL of the dashboard |
| `guide` | the reference for agents (`docs/agent-guide.md`) |
| `propose_operator` / `propose_types` | add or replace a draft module, in checked stages |
| `save_pipeline` / `list_pipelines` / `get_pipeline` / `delete_pipeline` | saved, validated specs |
| `crystallize` / `wait_crystallize` | a pull request that makes drafts stable; a long gate goes on in the background |
| `reload` | restart the kernel after a merge or manual edits |
| `environment` | Python, PATH, packages (and duplicate builds), binaries of `bin:` caps, as operators see them |
| `server_status` | the code this server process runs; `stale` when the code on disk is newer (reconnect the server) |

## Specialize a clone

1. Clone this repository (or use it as a GitHub template) under the
   domain's name.
2. Rename the packages: `domain/` → `<pkg>/` and `domain_draft/` →
   `<pkg>_draft/` (the draft package is always the stable one plus
   `_draft`). Update the drafts lines in `.gitignore` to the new name.
3. Edit `ico.toml`: `name`, `package = "<pkg>"`, and
   `github = { repo = "owner/<pkg>-mcp", base = "main" }` for
   crystallization. `env` lists the variables the operators may read
   (e.g. `HF_HOME`); others never reach them. `timeouts.wait` (45 s) is the
   longest a tool call waits for a run or a crystallization: keep it below your client's
   tool-call timeout (about 60 s in the Claude app), which restarts the
   server and loses its runs.
4. Edit `pyproject.toml`: the project `name`, and add the domain's
   dependencies (e.g. torch); operators run with the same environment.
5. Delete the demo draft `domain_draft/demo.py` and its example.
6. Write the domain's notes into section 8 of `docs/agent-guide.md`: data
   locations, formats, conventions, typical pipelines. Agents read it with
   the `guide` tool.
7. Keep `tests/test_domain_examples.py`: it runs the examples of every
   stable operator and is part of the crystallization gate.

Server fixes reach a clone through git:

```bash
git remote add template https://github.com/apriori3d/ico-mcp.git
```

```bash
git pull template main
```

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
  domain_types.py, type_names.py, pipelines.py, traces.py, progress.py, profiles.py
domain/ domain_draft/ pipelines/ examples/ runs/   the domain (runs/ is ignored)
```

The server process never imports operator code; only kernels do. Tests use
local bare git repositories as remotes and a fake `gh` executable.
