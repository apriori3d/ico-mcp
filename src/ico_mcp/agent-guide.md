# ico-mcp: guide for agents

This server builds and runs data pipelines from a registry of typed ICO
operators. Operators are not MCP tools: you find them as **cards** with
`search_operators`, combine them into a JSON **spec**, and run the spec.
When a step is missing you **write** the operator (and its types) as a
draft. Drafts that prove useful are **crystallized** into stable operators
through a pull request the human reviews.

Read sections 0–2 before your first spec. Read section 4 before your first
`propose_operator`.

## 0. Quick start

```
list_pipelines ──found──→ run(pipeline=name, input, params)
      │ none
      ▼
search_operators (input_type / output_type / text / tags)
      │ a step is missing
      ├────────────→ list_types → propose_types → propose_operator (+ examples)
      ▼
validate (fix every error) → run (small input first, then wait=false)
      ▼
save_pipeline → ask the human → crystallize → (merge) → reload
```

Minimal session:

1. `list_pipelines` (with each pipeline's parameters). One fits → run it
   by name: `run(pipeline="split_scenes", input=..., params={...})`.
   Empty, or nothing fits → compose.
2. `search_operators()` with no arguments lists the whole registry (cards
   carry `total`); then search by type: `input_type="str"`,
   `output_type="VideoRef"`.
3. Write the spec, `validate` it, `run` it on one small input.
4. Missing step → write it (section 4), call `validate` again.

## 1. Mental model

**A pipeline is one function from a JSON input to JSON outputs.**

- `run(spec, input)` passes `input` to the first node **as is**: a JSON
  string stays `str`, an object stays `dict`. Pass a path as a plain
  string (`/data/a.mp4`), not quoted (`"\"/data/a.mp4\""` arrives with
  the quotes). It is **never decoded into a
  domain type**. A flow that works on a `VideoRef` starts with an operator
  `str → VideoRef` (or `dict → VideoRef`).
- The last node must return JSON-compatible values (str, int, float,
  bool, None, list, dict; tuples become lists). A dataclass output is an
  error: `output of type V is not JSON-compatible`. End the flow with an
  operator that writes the result to disk and returns its path or a short
  summary dict.
- Between nodes any Python object flows (numpy arrays, models, frames).
  Only the two ends are JSON.

**Many items are an `Iterator`, not a `list`.**

- `seq` feeds one output to the next input. It never loops.
- To apply a step to every item, the producer returns `Iterator[T]` and
  the step goes inside a `stream` node: `Iterator[T] → Iterator[U]`.
- `list[T]` does **not** feed a `stream`:
  `expected Iterator[str], got list[str]`. Producers that make many items
  return `Iterator[T]`; an operator that needs all items at once takes
  `Iterator[T]` (or a `list[T]` from an operator that collects them).
- A top-level iterator output is consumed to the end; `run` returns the
  first `max_items` items, `total` and `truncated`.

**Typing is checked at the links.** Each link `A → B` needs
`A.output` to match `B.input` exactly or as a subclass; generic arguments
must be equal (`list[bool]` does not match `list[int]`). `validate`
reports every mismatch; `run` refuses to start on the first one.

**Where the code runs.** Each `run` starts a kernel process of its own
with the server's Python environment, in the server repository's root as
the working directory, with an allowlisted environment (`PATH`, `HOME`,
`TMPDIR`, `LANG`, `LC_ALL`, `VIRTUAL_ENV` plus `ico.toml` `env`).
Consequences:

- **Use absolute paths** in inputs and outputs: relative paths resolve
  against the server repository, not your project.
- External programs on `PATH` (e.g. `ffmpeg`) can be called with
  `subprocess`.
- Python packages are those of the server project. A missing package
  (`load` stage: `ModuleNotFoundError`) is not yours to install: tell the
  human which package to add to the server's `pyproject.toml`; after
  `uv sync` call `reload`.
- After the server's code changed (a pull, an update), `reload` loads the
  new operators, but the server process keeps its old tools. `reload`
  and `server_status` report it as `stale`: ask the human to reconnect
  (restart) the MCP server. A tool named in this guide but missing from
  your list means the same.
- Runs time out after `ico.toml` `timeouts.run` seconds (default 600);
  long runs go with `wait=false`.

## 2. Specs

A spec is JSON: a single node, or a document `{"version": 1, "root": node}`.
Every node has a `kind`; most have `children` and `params`; any node may
have a `name` (shown on the dashboard and in errors).

Leaves are registered operators: `{"kind": "op", "op": "name@version"}`.
A class operator takes its constructor arguments as `params` (the card
lists them under `params` with type, `required` and `default`):
`{"kind": "op", "op": "resize@1", "params": {"width": 256}}`.
Never `ref`, never a function name: only `op` with `name@version`.

| kind | children | params | does |
|---|---|---|---|
| `op` | – | constructor args of a class operator | a registered operator |
| `seq` | ≥ 2 | – | chain: each output feeds the next input |
| `pipeline` | ≥ 1 | – | chain of `T → T` steps on the same type |
| `stream` | 1 | – | apply the child to each item: `Iterator[I] → Iterator[O]` |
| `async_stream` | see below | `pool_size` (int), `ordered` (bool), `factory` (bool), `on_error` (`raise` / `skip` / `collect`), `retry` | parallel `stream` over a pool of workers |
| `worker` | 1 | `type`: `"ico.runtime.worker.mp.mp_worker:MPWorker"` | run the child flow in a separate process |
| `fork` | ≥ 2 | – | feed one input to every branch, return a tuple of outputs |
| `route` | key op, one per key, + default | `keys` (list of JSON scalars), `default` (bool) | the key op maps an item to a key; the item goes to that key's branch |
| `filter` | 1 (predicate `T → bool`) | – | keep the stream items for which the predicate is true |
| `partition` | 1 (predicate) | – | split a stream into `(matching, rest)` lists |
| `batcher` | – | `batch_size` (int), `drop_last` (bool) | group stream items into batches |
| `process` | 1 (body) | `num_iterations` (int) | apply the body that many times |
| `process` | 2 (body, until) | `max_iterations` (int), `on_limit` (`return` / `raise`) | apply the body until the predicate is true on its output |
| `progress` | – | `total` (number; omit when unknown) | count the items passing by (dashboard, MCP progress) |
| `identity` | – | – | pass items through |

Any other `kind` fails with `Cannot build node of kind '…' yet`: there is
no `map` / `foreach` / `par`; use `stream`, `async_stream` and `fork`.

`async_stream`: with `"factory": true` it has one child, the worker flow,
and every pool slot gets a fresh copy; with `"factory": false` it has
exactly `pool_size` children. `retry` is `{"max_retries": int, "delay":
number, "backoff": number, "retry_on": ["builtins:TimeoutError"]}`
(builtin exception types only).

### Parameters

A param value may be a **pipeline parameter** instead of a fixed value,
given per run:

```json
{"kind": "op", "op": "cut_clip@1",
 "params": {"out_dir": {"$param": "out_dir"},
            "threshold": {"$param": "threshold", "default": 27.0}}}
```

- `run(..., params={"out_dir": "/tmp/v02"})`; parameters without a
  `default` are required, `default` may be `null`. Unknown or missing
  names are errors that list them all.
- `input` is the data of a run; parameters are its configuration.
  Parametrize what changes between runs of a saved pipeline: output
  locations, thresholds you try, binaries (`ffmpeg`), run sizes
  (`pool_size`, `batch_size`, `progress.total`). Not per-item data (that
  is `input` or an operator's output) and not secrets (they come from the
  environment allowlist).
- A name used by several nodes must be declared the same way (same
  default). Only whole values: no substitution inside strings.
- `validate(spec, params)` lists the parameters (`params` in the result,
  with types from the operator classes) and needs values for those
  without a default.

### Patterns

Per-item work with progress (put `progress` **inside** the stream):

```json
{"kind": "seq", "children": [
  {"kind": "op", "op": "list_videos@1", "params": {"pattern": "*.mp4"}},
  {"kind": "stream", "children": [{"kind": "seq", "children": [
    {"kind": "progress", "name": "videos"},
    {"kind": "op", "op": "remove_subtitles@1"}
  ]}]}
]}
```

A progress node without `total` gets one from its stream when the
producer returns an iterator that knows its length: `iter(list)` or
`iter(range(...))`, not a generator. So an operator that knows how many
items it produces (scenes of a video, files of a directory) returns
`iter(items)`; the bar then shows a percentage and an ETA, per input.

The same in parallel processes, one copy of the flow per worker, per-item
errors collected instead of stopping the run:

```json
{"kind": "async_stream",
 "params": {"pool_size": 4, "factory": true, "ordered": true, "on_error": "collect"},
 "children": [{"kind": "worker",
   "params": {"type": "ico.runtime.worker.mp.mp_worker:MPWorker"},
   "children": [{"kind": "op", "op": "remove_subtitles@1"}]}]}
```

To count the items of an iterator that an earlier node produced (e.g. the
output of an `async_stream`), wrap the progress node in a `stream`:
`{"kind": "stream", "children": [{"kind": "progress", "params": {"total": 7}}]}`.
A bare progress node after an iterator counts it as one item; `validate`
warns about it (`counts_iterator`).

Two analyses of one input: `fork` returns a tuple, i.e. a JSON list.

## 3. Errors

Errors are JSON. Read the fields; do not guess.

- `validate` issues: `severity` (`error` / `warning`), `path` (the node in
  the spec tree: `"1"` is the second child of the root, `"0.2"` the third
  child of the first child), `expected` and `got` (types), `refs` (the two
  operators of the link) and a readable `message`.
  A warning "signature could not be inferred" means a link was not
  checked; the run may still work.
- `run`: `spec_error` (the spec does not build, or a non-JSON output),
  `run_error` (an exception; `traceback` is in the data), `timeout`,
  `crashed` (the process exited; `stderr` tail in the data). Per-item
  errors of `on_error: "collect"` streams are in the result's `errors`.
- `propose_operator` / `propose_types`: `stage` tells what failed:
  `write`, `typecheck` (pyright diagnostics with `file`, `line`, `rule`),
  `load` (import error, e.g. a missing package: see section 1),
  `register` (no operator, or `Any` in a signature), `types`, `examples`
  (each failing example with `expected` and `got`). The file stays in the
  draft package: fix it with another proposal of the **same module**.

Typical fixes:

| message | fix |
|---|---|
| `expected Iterator[X], got list[X]` | return `Iterator[X]` from the producer |
| `expected X, got Iterator[X]` | wrap the consumer in a `stream` |
| `expected VideoRef, got str` at the first node | start with a loader `str → VideoRef` |
| `output of type … is not JSON-compatible` | end with an operator that returns a path or a dict |
| `Cannot build node of kind 'map'` | use `stream` |
| `Operator 'x@1' is already registered` | bump the version in a new module |

## 4. Writing operators

Imports:

```python
from collections.abc import Iterator
from ico import IcoOperator  # base of class operators
from ico.registry import register  # the decorator
```

`register(name, version, *, tags=(), caps=(), description=None)`:

- `name`: no `@` or whitespace; `version`: a positive int.
- `tags`: free labels for search (`{"video", "ocr"}`).
- `caps` (capabilities): what the operator needs from its environment,
  shown on the card: `"gpu"`, `"network"`. An external program is
  `"bin:<name>"` (`"bin:ffmpeg"`): `get_operator` shows where it
  resolves, and `validate` warns (`missing_binary`) when it is not on the
  kernel's `PATH`.
- The first paragraph of the docstring is the card's `description`:
  write what it does and what the output means.
- Do not set `status`: the location decides it (draft package → `draft`).

**A function** when there is nothing to configure:

```python
@register("probe_video", 1, tags={"video"}, caps={"bin:ffprobe"})
def probe_video(video: VideoRef) -> VideoInfo:
    """Width, height, fps and duration of a video (ffprobe)."""
    ...
```

**A class** when the operator has configuration. Every `__init__`
parameter is stored in an attribute of the same name (the spec captures
them from there), no `*args` / `**kwargs`, JSON-compatible defaults:

```python
@register("detect_text", 1, tags={"video", "ocr"}, caps={"gpu"})
class DetectText(IcoOperator[FrameRef, TextBoxes]):
    """Text boxes in a frame, by an OCR detector."""

    def __init__(self, languages: list[str], min_score: float = 0.5) -> None:
        self.languages = languages
        self.min_score = min_score
        self._model: Detector | None = None  # loaded on first call
        super().__init__(fn=self._apply)

    def _apply(self, frame: FrameRef) -> TextBoxes:
        if self._model is None:
            self._model = load_detector(self.languages)  # lazy: once per worker
        ...
```

Instances are created whenever a flow is built (once per worker), so
heavy work never happens at import or in `__init__`.

**A producer** of many items returns an iterator:

```python
@register("list_videos", 1, tags={"video", "io"})
class ListVideos(IcoOperator[str, Iterator[VideoRef]]):
    """Videos in a directory matching a glob pattern, sorted by name."""

    def __init__(self, pattern: str = "*.mp4") -> None:
        self.pattern = pattern
        super().__init__(fn=self._apply)

    def _apply(self, directory: str) -> Iterator[VideoRef]:
        for path in sorted(Path(directory).glob(self.pattern)):
            yield VideoRef(path=str(path.resolve()))
```

**Boundary operators.** A flow starts with a loader from JSON (`str`,
`dict`) and ends with a writer to JSON (a path, a summary `dict`). Writers
take the output location as a param (`out_dir`) and return absolute paths.

Rules:

- One small step per operator, typed input and output. No `Any`.
- Configuration in constructor params, never in closures or globals.
- Never edit a registered version that others may use: register
  `name@2` in a new draft module.
- **External programs**: make the binary a param with the plain name as
  default (`ffmpeg: str = "ffmpeg"`), so a pipeline can pin a full path
  (`$param`); declare `bin:<name>`; on failure raise with the command's
  stderr, not only its exit code. What the binary supports (codecs) is
  the operator's own check.
- **Heavy frameworks** (torch, paddle, tensorflow) are imported inside the
  operator on first call, never at module level: two frameworks in one
  process can clash (two OpenMP runtimes crash together). A run's
  process imports only the modules of the operators its pipeline uses
  (and what they import), and workers only their own; so keep operators
  that need clashing frameworks in different modules, and in different
  workers. `environment()` shows the
  packages, and `duplicates` lists packages installed by several
  distributions (e.g. three OpenCV builds sharing `cv2`).
- Keep `typecheck` on (pyright strict). Untyped third-party libraries
  need `cast` or a narrow wrapper typed by you.
- **Replacing a draft that saved pipelines use.** A new proposal of a
  module reports `changes` (operators whose `sig` or `params` changed, or
  that are gone, with the saved pipelines using them) and re-validates
  those `pipelines`. Fix a pipeline that no longer validates, or register
  a new version (`@2`) in a new module when the old behaviour is still
  needed. Draft cards carry a `fingerprint` of their module; run traces
  record it, so the crystallize PR can tell runs on earlier draft code.
- **Small fixes as edits.** `code` is for a new module or a rewrite. To
  change an existing draft, send `edits=[{"old": ..., "new": ...}]`
  (also for `propose_types`): replacements applied in order, each `old`
  occurring exactly once in the module (`get_operator` gives a draft's
  `module_source`); an empty `new` deletes. A failed edit changes
  nothing and names the edit.
- **Refactoring across modules.** A new module cannot register an
  operator name an old module still holds: move the operators, then
  `delete_module(module)` removes the old draft (and the examples of its
  operators) and reports the saved pipelines that used them, validated
  again. It refuses a module other drafts still import (`imported_by`)
  and stable modules (they leave by a pull request).

**Examples** (`examples=[{"ref", "input", "output"}]`) are run as the
spec `{"kind": "op", "op": ref}` on `input`, and must give exactly one
output equal to `output`. They become the operator's tests on
`crystallize`, so they are for **pure operators**: JSON input, JSON
output, no files or environment (parsing a manifest, computing cut
points, formatting a report). Give them for those.

Operators on domain data (reading a video, cutting clips, returning a
dataclass) need no examples: an example would depend on files that exist
only on this machine. Check them with a `run` of a pipeline on a small
real input; the crystallize pull request shows their recent successful
runs (input, outputs, errors, duration) as the evidence.

## 5. Types and imports

- Look for a type first: `list_types`, `get_type(name)`.
- New types go into a **types-only** module with `propose_types`:
  immutable `@dataclass(frozen=True)` or Pydantic models, with a
  docstring. Operator modules import them.
- Large data at the pipeline boundary goes by reference: a type such as
  `VideoRef(path: str)`, never bytes. Inside a pipeline it may stay in
  memory (`Frame` with a numpy array).
- Imports between drafts are **relative** (`from .media import VideoRef`);
  imports of the stable package are **absolute**
  (`from <package>.media import VideoRef`). An absolute import of the
  draft package is rejected at the `write` stage.

```python
# propose_types(module="media", code=...)
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoRef:
    """A video file on disk, by absolute path."""

    path: str
```

## 6. Runs and the dashboard

- `run(spec, input)` (or `run(pipeline=name, input, params)` for a saved
  pipeline) returns `outputs`, `total`, `truncated`, per-item `errors`,
  the `run_id` and the `dashboard` URL. The dashboard titles a run by its
  pipeline (or root node) name and its parameter values.
- First run on the smallest input (one file, a few seconds of video).
- Long run: `run(..., wait=false)` returns the `run_id` at once; a run
  with `wait=true` that is still going after `timeouts.wait` (45 s by
  default) returns `state: running` with `waited` too, and goes on in the
  background. Then `wait_run(run_id)` waits on the server until the run
  ends or `timeouts.wait` passes, and returns its status (state
  `running` / `done` / `failed`, progress of every node, `result` or
  `kernel_error` once finished). Still `running`: call it again. No sleep
  loops; `run_status` is the same snapshot without waiting. Tool calls
  stay below the client's tool-call timeout (about 60 s in the Claude
  app): a call that exceeds it makes the client restart the server, and
  the run is lost.
  `cancel_run(run_id)` stops a run between items and returns its status
  with the outputs produced so far (`result`, with `cancelled`). A long
  item in a worker stops between its own inner items (frames of a video);
  an operator stuck in one call is killed after 10 s, without a result.
- What flows between operators: `sample_run(run_id, node, count=3)`
  returns short previews (`type`, `preview`) of the next `count` (up to
  20) results of `node` in a running run. `node` is a plan key (`0.1.2`,
  the keys of the dashboard plan) or a label (`resize@1`, or `resize`
  when one version is used); an unknown or ambiguous label lists the
  plan's nodes. A stream or source previews as an iterator: sample the
  operator inside the stream. Arrays show as shape and dtype, dataclasses
  field by field. It waits at most `timeouts.wait`; `complete: false`
  means fewer results came (a slow node, or the run ended). `not_running`:
  the run has ended or not started yet.
  Results that already passed a node: start a long run with
  `run(..., recent=true)` (about one preview per second of every
  operator, a clock read per call); `sample_run` then returns the node's
  last ones in `recent`, and `wait=false` returns them at once.
- Give the human the dashboard URL; it shows every progress node and the
  plan of active and recent runs.

### Profiling a slow run

`run(..., profile=true)` measures every node; the overhead is about 4 µs
per call or item, so profile runs that are slow, not every run. The result
gets `profile`:

```json
{"elapsed": 2.1, "overhead": 0.13,
 "top": [{"key": "0.4.0.0.2", "label": "FetchCifarItem()", "self": "6.21s",
          "share": "66%", "count": "2000", "p95": "11µs",
          "info": "first call 6.19s"}]}
```

`top` lists the nodes with the largest self time, up to 5; the dashboard
shows every node on the plan (columns after Flow).

- `self`: time of the node itself, without the nodes it calls. `share`: of
  all work, summed over threads and worker processes.
- `p95`: a call or item in the steady state. The first call of each
  operator instance and the first item of each iterator are kept out of
  it; `first call` in `info` shows them when they are far slower than the
  steady state (their mean, `×N` for one per worker). A slow first call is
  lazy setup (a model or a dataset loaded in the first call, in every
  worker process): load it once, share it (e.g. a dataset in shared memory
  created before the workers), or accept it for long runs.
- `util N%` (async streams): how busy the pool was. Low: the input is the
  bottleneck, so more workers will not help. High: the workers are.
- `transfer` (workers): time spent passing items to and from the worker
  process. Large compared to the worker's work: pass references (paths),
  not data, or do more work per item (batches).
- `max` in `info`: a rare slow call far above p95.

## 7. Saving and crystallizing

- `save_pipeline(name, spec, description)` when the pipeline will be
  reused; it must validate. `replace=true` to overwrite. The description
  is what the next agent reads in `list_pipelines`: what the pipeline does,
  its input and output, which pipeline comes before it and which after,
  and when to prefer it over an alternative. Knowledge across pipelines
  (the whole workflow, data locations, hardware) goes to the domain notes,
  `docs/domain.md` in the domain repository, which `guide` serves below. Give `params`
  for its parameters without a default: they only validate it (saved as
  `check_params`, also used by the crystallize gate), never as run
  values.
- `delete_pipeline(name)` removes a saved pipeline that is no longer
  useful (e.g. an experiment), so `list_pipelines` stays short.
- `crystallize(refs, title, summary, pipelines)` opens a pull request that
  makes the drafts stable. It pushes a branch: **ask the human first**.
  A crystallization still going after `timeouts.wait` (the gate can take
  minutes on a large domain) returns `state: running` with a
  `crystallize_id` and its `stage` (`check`, `git`, `gate`, `publish`)
  and goes on in the background; call `wait_crystallize(crystallize_id)`
  until it returns the pull request or a failed stage. Proposals may go on
  meanwhile: the gate works on a copy. After the human merges and pulls,
  call `reload`.
