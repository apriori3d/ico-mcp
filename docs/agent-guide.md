# ico-mcp: guide for agents

This server builds and runs data pipelines from a registry of typed ICO
operators. The server `instructions` are the short version; this guide is
the reference. Read the part you need.

## 1. Workflow

1. **Saved pipeline first.** `list_pipelines`, then `get_pipeline(name)`.
   If one fits, `run` it as is.
2. **Compose from existing operators.** Search step by step:
   `search_operators(input_type=..., output_type=...)` finds direct
   converters; `text` and `tags` narrow the search. Cards show `sig`
   (`str → list[str]`), `description`, `tags`, `status`
   (`draft` / `stable`). `get_operator(ref)` adds the source code: read it
   only to write or fix an operator.
3. **Validate, then run.** `validate(spec)` reports every type mismatch at
   once, with the location in the spec. Fix all errors before `run`.
   Warnings ("signature could not be inferred") mean a link was not
   checked; the run may still work.
4. **Write an operator only when a step is missing**
   (`propose_operator`, section 4). Reuse types first (section 5).
5. **Save what works.** When a pipeline is likely to be reused:
   `save_pipeline(name, spec, description)`.
6. **Crystallize with the human's consent.** Suggest `crystallize` for the
   draft operators of a pipeline that proved useful. It pushes a branch and
   opens a pull request, so ask first. After the human merges and pulls,
   call `reload`.

Types are referred to by short name (`Report`), by `module:qualname`
(`domain.text:Report`) when a short name is ambiguous (the error lists the
candidates), or as cards print them (`list[Report]`).

## 2. Specs

A spec is JSON: a single node, or a document `{"version": 1, "root": node}`.
Every node has a `kind`; most have `children` and `params`; any node may
have a `name` (shown on the dashboard and in errors).

```json
{"kind": "seq", "children": [
  {"kind": "op", "op": "load_text@1"},
  {"kind": "op", "op": "split_words@1"}
]}
```

Leaves are registered operators: `{"kind": "op", "op": "name@version"}`.
An operator registered as a class takes its constructor arguments as
`params`: `{"kind": "op", "op": "resize@1", "params": {"width": 256}}`.
Never `ref`, never a function name: only `op` with `name@version`.

| kind | children | params | does |
|---|---|---|---|
| `op` | – | constructor args of a class operator | a registered operator |
| `seq` | ≥ 2 | – | chain: each output feeds the next input |
| `pipeline` | ≥ 1 | – | chain of `T → T` steps on the same type |
| `stream` | 1 | – | apply the child to each item: `Iterator[I] → Iterator[O]` |
| `async_stream` | see below | `pool_size` (int), `ordered` (bool), `factory` (bool), `on_error` (`raise` / `skip` / `collect`, default `raise`), `retry` (optional) | parallel `stream` over a pool of workers |
| `worker` | 1 | `type`: `"ico.runtime.worker.mp.mp_worker:MPWorker"` | run the child flow in a separate process |
| `fork` | ≥ 2 | – | feed one input to every branch, return a tuple of outputs |
| `route` | key op, one per key, + default | `keys` (list of JSON scalars), `default` (bool) | the key op maps an item to a key; the item goes to that key's branch |
| `filter` | 1 (predicate) | – | keep the stream items for which the predicate is true |
| `partition` | 1 (predicate) | – | split a stream into `(matching, rest)` lists |
| `batcher` | – | `batch_size` (int), `drop_last` (bool) | group stream items into batches |
| `process` | 1 (body) | `num_iterations` (int) | apply the body that many times |
| `process` | 2 (body, until) | `max_iterations` (int), `on_limit` (`return` / `raise`) | apply the body until the predicate is true on its output |
| `progress` | – | `total` (number) | count the items passing by (dashboard, MCP progress) |
| `identity` | – | – | pass items through |

`async_stream`: with `"factory": true` it has one child, the worker flow,
and every pool slot gets a fresh copy (`pool_size` copies); with
`"factory": false` it has exactly `pool_size` children. `retry` is
`{"max_retries": int, "delay": number, "backoff": number, "retry_on":
["builtins:TimeoutError"]}` (builtin exception types only).

**Progress.** A progress node counts the items that pass it, so put it
inside the stream it measures, with a `total` if known:

```json
{"kind": "seq", "children": [
  {"kind": "op", "op": "words@1"},
  {"kind": "stream", "children": [{"kind": "seq", "children": [
    {"kind": "progress", "name": "words", "params": {"total": 40}},
    {"kind": "op", "op": "upper@1"}]}]}
]}
```

Before the stream it would count the whole iterator as one item. The first
progress node of a flow is also reported as MCP progress.

**Parallel work in processes.** Wrap the per-item flow in a `worker` inside
an `async_stream` with `"factory": true`; register heavy operators as
classes that load models lazily (on first call), so each worker loads its
own copy.

## 3. Errors

Errors are JSON. Read the fields; do not guess.

- `validate` issues: `severity` (`error` / `warning`), `path` (the node in
  the spec tree: `"1"` is the second child of the root, `"0.2"` the third
  child of the first child), `expected` and `got` (types), `refs` (the two
  operators of the link) and a readable `message`:
  `at 1 (count_words@1 → shout@1): expected str, got int`.
- `run`: `spec_error` (the spec does not build: same fields as above),
  `run_error` (an exception; `traceback` is in the data), `timeout` (the
  run's kernel was killed after the configured timeout), `crashed` (the
  process exited; `stderr` tail in the data). Per-item errors of
  `on_error: "collect"` streams are in the result's `errors`.
- `propose_operator` / `propose_types`: `stage` tells what failed:
  `write` (module name, or an absolute import of the draft package: use the
  `fix` it gives), `typecheck` (pyright diagnostics with `file`, `line`,
  `rule`), `load` (import error), `register` (no operator, or `Any` in a
  signature), `types` (no public class, or operators in a types module),
  `examples` (each failing example with `expected` and `got`). The file
  stays in the draft package: fix it with another proposal of the same
  module.

## 4. Writing operators

```python
from ico.registry import register


@register("slugify", 1, tags={"text"})
def slugify(text: str) -> str:
    """Lower-case words joined by dashes."""  # the card's description
    return "-".join(text.lower().split())
```

- One small step per operator, typed input and output. No `Any`: the
  registry matches operators by type, and `Any` matches nothing usefully.
- Configuration goes into constructor params of a class operator, never
  into closures or globals, so a spec can carry it:

  ```python
  from ico.core.operator import IcoOperator
  from ico.registry import register


  @register("resize", 1, tags={"image"})
  class Resize(IcoOperator[ImageRef, ImageRef]):
      """Resize an image to a width, keeping the aspect ratio."""

      def __init__(self, width: int) -> None:
          self.width = width
          super().__init__(fn=self._apply)

      def _apply(self, image: ImageRef) -> ImageRef: ...
  ```

- Heavy models load lazily, on first use, not at import or construction.
- Return iterators (`Iterator[T]`) for operators that produce many items;
  streams process them item by item.
- Give `examples` (`{"ref", "input", "output"}`) for operators with JSON
  inputs and outputs. They are checked on proposal, saved, and become the
  tests of the operator when it is crystallized.
- A stable operator is never edited: register a new version
  (`@register("slugify", 2)`) in a new draft module.
- `typecheck` (pyright) is on by default; keep it on.

## 5. Types and imports

- Look for a type before writing one: `list_types`, `get_type(name)`
  (fields and docstring).
- New types go into a types-only module with `propose_types`: immutable
  dataclasses (`@dataclass(frozen=True)`) or Pydantic models, with a
  docstring.
- Large data crosses the pipeline boundary by reference: a path or URI in
  a type such as `ImageRef(path: str)`, never bytes in JSON. Inside a
  pipeline, data may stay in memory (e.g. a `Frame` with a pixel array).
- Draft modules import each other **relatively**
  (`from .frames import Frame`) and the stable package **absolutely**
  (`from <package>.frames import Frame`). Absolute imports of the draft
  package are rejected. This lets crystallization move files unchanged.

## 6. Runs and the dashboard

- `run(spec, input)` returns `outputs` (the first `max_items` items of an
  iterator output), `total`, `truncated`, per-item `errors`, the `run_id`
  and the `dashboard` URL. Inputs and outputs are JSON; pass paths.
- Each run has a process of its own. For a long run use
  `run(..., wait=false)`: it returns the `run_id` at once. Poll
  `run_status(run_id)` (state `running` / `done` / `failed`, progress of
  every node, `summary`, and `result` or `kernel_error` once finished);
  stop it with `cancel_run(run_id)`. Search, validate and proposals keep
  working while a run is going.
- The dashboard shows every progress node and the pipeline plan of active
  and recent runs. Give the human its URL (`dashboard` tool, or the
  `dashboard` field of a run result); it opens in any browser, and in the
  desktop app's browser pane.

## 7. Domain notes

This section belongs to the domain. A clone of this repository describes
here its data, formats, conventions and typical pipelines, e.g. where the
input files live, which types to use for them, and naming rules for
operators.
