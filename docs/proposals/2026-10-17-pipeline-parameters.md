# Implementation spec: pipeline parameters in ico-mcp

**Status.** Implemented in ico-mcp#1 (with apriori3d/ico#36), as specified,
including `check_params`.

Date: 2026-10-17. Covers items 4 (run a saved pipeline by name), 5
(pipeline parameters) and the title part of item 6 of
[2026-10-17-first-domain-session.md](2026-10-17-first-domain-session.md).
One roadmap item; the design below is the proposal to approve before
coding (`CLAUDE.md`).

**Depends on** apriori3d/ico#36 (ICO core: `$param` references,
`ico.spec.bind`, `ico.spec.spec_params`, `params=` in `build` / `run`).
Merge it first; nothing below re-implements substitution in ico-mcp.

## 1. What a parameter is (for the reviewer and the guide)

`input` is the data of a run; parameters are its configuration. Both are
given per run, and a saved pipeline stays one document:

```json
{"kind": "op", "op": "cut_clip@1",
 "params": {"out_dir": {"$param": "out_dir"},
            "ffmpeg": {"$param": "ffmpeg", "default": "ffmpeg"},
            "threshold": {"$param": "threshold", "default": 27.0}}}
```

```
run(pipeline="split_scenes", input="/videos/02.mp4",
    params={"out_dir": "/tmp/v02", "threshold": 20})
```

ICO substitutes the values before building, so operators receive ordinary
constructor arguments. Parameters are not global state and not
environment variables (secrets still go through the `env` allowlist).
Scenarios from the first session: an output directory per run, trying
thresholds without saving copies of a pipeline, pinning a binary
(`ffmpeg`), and run sizes (`pool_size`, `batch_size`, `progress.total`,
which may now be omitted: apriori3d/ico#34).

## 2. Kernel (`kernel.py`)

All substitution and declaration logic is ICO's; the kernel passes values
and maps errors.

- `params` (optional, an object) is accepted by `validate` and `run`.
  Anything else than an object is `bad_request`.
- `run`: `ico.spec.run(spec, input, params=params, ...)`. `_send_plan`
  builds with the same params (the plan shows the bound values); it stays
  silent when binding or building fails.
- `validate`: bind first, then build and check signatures as now. Returns
  `{"issues": [...], "params": [...]}` where `params` is
  `[p.to_dict() for p in spec_params(spec)]` (name, type, required,
  default, paths), computed on the **unbound** spec, so the caller sees
  what the pipeline accepts even when values are missing.
- Binding errors (`SpecError`: unknown or missing names, malformed
  reference, conflicting declarations) are reported like other spec
  errors: a `spec_error` from `run`; an issue with `severity: "error"` from
  `validate` (its `path` is set for a malformed reference or a conflict,
  `null` for missing / unknown names). `params` is still returned when
  `spec_params` itself succeeds.
- A new method is not needed: `validate` returns the declarations.

## 3. Server tools (`server.py`, `jobs.py`)

### `run`

```
run(spec=None, pipeline=None, input=None, params=None, max_items=None, wait=True)
```

- Exactly one of `spec` and `pipeline`; both or neither is a
  `bad_request` tool error. `pipeline` loads `pipelines/<name>.spec.json`
  (`not_found` lists the saved names).
- `params` is passed to the run's kernel.
- `input` and `params` get an explicit JSON schema (item 2 of the session
  proposal, same change): `anyOf` of string, number, integer, boolean,
  object, array, null, described as "a JSON value; a path is a plain
  string". The docstring says to pass a string as is, not quoted.
- The result is unchanged (`outputs`, `total`, `truncated`, `errors`,
  `run_id`, `dashboard`).

### `validate`

`validate(spec, params=None)` returns `{"issues", "params"}` from the
kernel (section 2). The docstring says: give values for parameters without
a default, or they are reported as missing.

### `save_pipeline`

`save_pipeline(name, spec, description, replace=False, params=None)`:

- The spec is saved with its `$param` references.
- It must validate without errors **with `params`**: values for
  parameters without a default are needed to build it. They are stored in
  the document as `check_params` (section 4), never used as run values.
- The result adds `params` (the declarations).

### `get_pipeline` / `list_pipelines`

- `get_pipeline(name)` adds `params` (declarations from `validate` of the
  stored spec; computed by the kernel, the server never imports ICO's
  spec code for this) and `check_params`.
- `list_pipelines` adds per pipeline the parameter names with
  `required` and `default`, so an agent sees what to pass without a second
  call. If computing them fails (e.g. an operator was removed), the entry
  has `params: null` and an `error` message; listing never fails.

### Titles (`jobs.py`)

Run title on the dashboard and in `run_status` (item 6):

1. the pipeline name when run by `pipeline`;
2. else the root node `name` of the spec;
3. else the op refs, as now;

followed by ` · ` and the given `params` as `key=value`, the whole title
cut to 120 characters. `RunState` / `BoardRun` stay as they are; only the
title string changes.

### Traces (`traces.py`)

Each trace line adds `pipeline` (name or `null`) and `params` (the given
values, shortened like `input`). Crystallize trace statistics are
unchanged (per operator ref).

## 4. Pipeline documents (`pipelines.py`)

```json
{"version": 1, "description": "...", "root": {...},
 "check_params": {"out_dir": "/tmp/check"}}
```

- `check_params` (optional) holds values for the parameters without a
  default, used only to validate the pipeline: by `save_pipeline` and by
  the crystallize gate step "validate pipelines". Without it, a pipeline
  with a required parameter could never be validated again.
- `pipelines.document(spec, description, check_params)` writes it when
  non-empty; old documents without it stay valid.
- **Crystallize gate**: `_gate_load_and_pipelines` passes the document's
  `check_params` to `validate`. A missing value is then a gate failure
  that names the pipeline and the parameter.

Decision to confirm: the alternative is to require a default for every
parameter of a *saved* pipeline (no `check_params`). Simpler, but a
parameter like `out_dir` has no meaningful default, and a fake default
hides a forgotten value at run time. This spec proposes `check_params`.

## 5. Instructions and guide

- Server `instructions`, step 1: "run a saved pipeline with
  `run(pipeline=name, input=..., params=...)`; `list_pipelines` shows its
  parameters".
- `docs/agent-guide.md`:
  - section 2 (Specs): the `$param` syntax, `default` (may be `null`), one
    declaration per name, whole values only;
  - when to parametrize: output locations, thresholds and tunables tried
    across runs, binaries, run sizes; not per-item data (that is `input`
    or an operator's output); not secrets (`env` allowlist);
  - section 1 / 6: run by name, `validate` / `save_pipeline` take
    `params` for parameters without a default;
  - the string-input rule (item 2).

## 6. Tests (no network, as always)

Kernel (`test_kernel.py`):
- `run` with `params` substitutes (an operator class param from the
  fixture repository); a default applies; `total` of a progress node
  given as a parameter.
- `validate` returns `params` declarations (name, type from the registry,
  required, default, paths) also when values are missing; missing /
  unknown names and a malformed reference are error issues.
- the `plan` notification shows bound values.

Server (`test_server.py`):
- `run(pipeline=...)` equals `run(spec=...)` of the same spec; both / neither
  is a `bad_request`; an unknown name is `not_found` with the saved names.
- the `run` tool schema lists the `anyOf` for `input` and `params`; a
  string input arrives unquoted.
- `save_pipeline` with a required parameter: rejected without `params`,
  saved with them; the document keeps the `$param` reference and
  `check_params`; `get_pipeline` and `list_pipelines` show the parameters.
- titles: pipeline name, root name, refs; params appended.
- traces carry `pipeline` and `params`.

Crystallize (`test_crystallize.py`):
- the gate validates a saved pipeline with a required parameter using its
  `check_params`; without them the gate fails and names the parameter.

## 7. Out of scope

- Substitution inside strings or nested values (ICO decision, YAGNI).
- Parameter types checked by the server: ICO reports types from the
  registry; the operator's constructor validates values.
- Saved runs or presets of parameter values.
- `delete_pipeline` (item 13) and the other items of the session proposal.
