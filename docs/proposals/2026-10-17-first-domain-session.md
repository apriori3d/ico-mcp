# Proposal: fixes from the first domain session (video scenes)

Date: 2026-10-17. Source: an agent session that built the first real
pipeline on this server — splitting videos into scene clips for subtitle
removal (`load_video → detect_scenes → scene_cuts → stream(cut_clip) →
scene_report`, saved as `split_scenes`). Every item below was hit in that
session; the evidence says how. Items are ordered by priority within each
group; each one is meant to become one roadmap item (one commit or a small
series), with a design note and tests as `CLAUDE.md` requires.

Status as of 2026-10-18 in the summary and under each item.

Legend: **[ico]** change in `../ico`, **[mcp]** change in this repository,
**[guide]** change in `docs/agent-guide.md` or the server instructions.

## Summary

| # | Item | Where | Priority | Status |
|---|---|---|---|---|
| 0 | A crashed `MPWorker` hangs its `async_stream` until the run timeout | ico | critical | done |
| 0a | `X \| Y` unions in class operator types break signature inference | ico | high | done |
| 3b | `progress.total` per item (from the item, e.g. a known scene count) | ico | medium | open |
| 7a | Domain dependencies that clash in one process (torch + Paddle, OpenCV builds) | mcp, guide | medium | done |
| 1 | The guide's import of `IcoOperator` fails pyright strict | guide, ico | high | done |
| 2 | `run.input` has no JSON schema: strings arrive with quotes | mcp | high | done |
| 3 | `progress.total` is required although documented as optional | ico, guide | high | done |
| 3a | Guide: count stream items with `stream(progress)` | guide | medium | done |
| 4 | Run a saved pipeline by name | mcp | high | done |
| 5 | Pipeline parameters (`$param`) | ico, mcp | high | done |
| 6 | Run titles ignore the spec `name` | mcp | medium | done |
| 7 | Environment report and binary capabilities | mcp | medium | done |
| 8 | Stale tool list after a server update is invisible to the agent | mcp | medium | done |
| 9 | Small edits of a draft module need the whole module | mcp | medium | done |
| 10 | Replacing a draft silently changes the meaning of runs and pipelines | mcp | medium | in review |
| 11 | Examples cannot test operators on domain types | mcp, guide | medium | done, revised |
| 12 | Progress inside a long operator | ico, mcp | low | open |
| 13 | Delete a saved pipeline | mcp | low | done |
| 14 | This repository is used as a domain directly | process | low | in progress |

## Interface bugs

### 0. A crashed `MPWorker` hangs its `async_stream` until the run timeout [ico]

**Status.** Done: apriori3d/ico#37.

**Evidence.** A text-scan run (`async_stream(factory, worker(MPWorker,
...))`) processed 25 scenes, then its worker process died with SIGSEGV
inside Paddle (macOS crash report at 22:53:38). Nothing was reported: the
kernel sat at 0 % CPU, the board showed the run as running, and the run
ended only when the kernel was killed after `timeouts.run` (600 s) with a
generic `timeout`. The cause (a native crash) was found only through
`~/Library/Logs/DiagnosticReports`.

**Proposal.** [ico] The parent side of `MPWorker` waits for results with a
liveness check (`Process.is_alive()` / `exitcode` polled while waiting, or
a sentinel `wait`), and turns a dead runner into an error carrying the
exit code or signal (`worker died: SIGSEGV`), handled by `on_error` like
any item error (`collect` lists it; `raise` fails the run at once). [mcp]
`run_status` and the board show it.

**Tests.** ico: a worker flow that calls `os.kill(os.getpid(), SIGSEGV)`
(or `os._exit(1)`) on the third item: with `raise` the run fails within a
second, with `collect` the other items still complete.

### 0a. `X | Y` unions in class operator types break signature inference [ico]

**Status.** Done: apriori3d/ico#38.

**Evidence.** Registering `class SceneStrips(IcoOperator[Report,
ReviewedReport])` with `Report = dict[str, str | int | list[int] |
list[Summary]]` failed at `load`: `TypeError: type 'types.UnionType' is
not subscriptable` in `ico/core/signature_utils.py:replace_typevar`
(`return origin[replaced_args]` with `origin` = `types.UnionType`). The
same aliases written with `typing.Union` work (they are equal at run time).
Function operators are not affected.

**Proposal.** [ico] In `replace_typevar`, rebuild a `types.UnionType` with
`functools.reduce(operator.or_, replaced_args)` (or `typing.Union[...]`).

**Tests.** ico: `replace_typevar` on `dict[str, int | list[I]]`; a class
operator with `|` unions in its generic base registers and links.

### 1. The guide's import of `IcoOperator` fails pyright strict [guide, ico]

**Status.** Done: apriori3d/ico#33, ico-mcp#1.

**Evidence.** The first `propose_operator` of a class operator failed at
`typecheck`: `Stub file not found for "ico.core.operator"`
(`reportMissingTypeStubs`). `ico/core` has no `__init__.py`: it is a
namespace package, and pyright does not apply the `py.typed` of `ico` to
it. Checked with the server's own checker (`ico_mcp.typecheck`):
`from ico import IcoOperator` gives no diagnostics,
`from ico.core.operator import IcoOperator` gives `reportMissingTypeStubs`.
The agent worked around it with `# pyright: ignore[...]`.

**Proposal.**
- [guide] Use `from ico import IcoOperator` in section 4 and in every
  example (now: `from ico.core.operator import IcoOperator`).
- [ico] Add `src/ico/core/__init__.py` (and the other namespace
  subpackages that public code imports, e.g. `ico/tools`, which
  `server.py` imports with an ignore) so that deep imports are typed too.

**Tests.** ico: a pyright strict run over a file that imports
`ico.core.operator.IcoOperator`. mcp: a proposal test whose module uses
the import form of the guide passes `typecheck`.

### 2. `run.input` has no JSON schema: strings arrive with quotes [mcp]

**Status.** Done: ico-mcp#1.

**Evidence.** `run(spec, input="\"/Users/…/01 interactive bumper car.mp4\"")`
reached the flow as a string *with* the quotes:
`FileNotFoundError: no video file at /Users/shum/dev/apriori/ico-mcp/"/Users/…mp4"`.
The tool schema has `"input": {"default": null}` without a type
(`JsonValue = None`), so the client cannot tell a JSON value from text and
passes what the agent typed verbatim. The same holds for
`OperatorExample.input` / `output`.

**Proposal.** Give `input` (and example `input` / `output`) an explicit
schema: `anyOf: [string, number, integer, boolean, object, array, null]`
with a description "a JSON value; a path is a plain string". Add to the
`run` docstring: "Pass a string as is, e.g. `/data/a.mp4`, not quoted."

**Tests.** The schema of `run` lists the `anyOf`; a run with a string
input gets exactly that string.

### 3. `progress.total` is required although documented as optional [ico, guide]

**Status.** Done: apriori3d/ico#34.

**Evidence.** `{"kind": "progress", "name": "scenes"}` failed `validate`:
`Param 'total' of 'progress' must be a number, got None` (`build_progress`
calls `_number(spec, "total")`). The guide and the server instructions say
"with a `total` if known". The number of scenes is known only at run time,
so the agent passed `"total": 0`; the board then showed an empty bar with
a counter.

**Proposal.** [ico] `total` optional: `None` means unknown; the board and
`run_status` show a counter and a rate without a bar or an ETA. Keep `0`
an error ("use no total for an unknown count"), so that a wrong total is
not hidden.

**Tests.** ico: build, describe and progress snapshots of a progress node
without `total`. mcp: a run with such a node reports `total: null`.

### 3a. Guide: count stream items with `stream(progress)` [guide]

**Status.** Done: apriori3d/ico#35 (`counts_iterator` warning), guide in ico-mcp#1.

**Evidence.** `list_videos → async_stream(worker(...)) → progress(total=7)`
ran all 7 videos, but the progress node ended at `done: 1, total: 7`: a
bare progress node after an iterator sees the iterator as one item. Wrapped
in a stream, `async_stream(...) → stream(progress(total=7))`, it counted
7/7 on the board, one bar per video. Inside the worker the node runs in
another process. The guide only says to put progress "inside the stream it
measures" and shows it before the per-item operator, so the agent did not
find the pattern for parallel streams.

**Proposal.** [guide] Section 2, Patterns: "to count the items of an
iterator (e.g. the output of an `async_stream`), wrap the progress node in
a `stream`", with the parallel example ending in
`{"kind": "stream", "children": [{"kind": "progress", "params": {"total": N}}]}`.
Optionally, [mcp] a `validate` warning for a progress node whose input is
an `Iterator` ("counts the iterator once; wrap it in a stream").

**Tests.** mcp: the validate warning for a bare progress node after an
iterator.

### 3b. `progress.total` per item [ico]

**Status.** Open.

**Evidence.** In `async_stream(worker(load_scene_clips → filter →
stream(progress → scan_text → ...)))` each worker gets one video; the
number of its scenes is known (it is in the scene manifest) but differs per
video (2 to 33), so the per-worker scene bars could only count
(`total: 0`), without a percentage or ETA.

**Proposal.** [ico] Let the item set the total: a progress node with
`"total": "len"` takes `len(item)` when its input is a sized collection, or
an operator can return an iterator that knows its length (`__len__` or
`__length_hint__`) and the progress node in the stream over it uses that.

**Tests.** ico: `stream(progress)` over a list-backed iterator with a
length hint reports that total.

### 4. Run a saved pipeline by name [mcp]

**Status.** Done: ico-mcp#1.

**Evidence.** To run `split_scenes` on video 02 the agent called
`get_pipeline` and copied the whole spec (≈1 KB of JSON) into `run`. Any
copy error goes unnoticed, and run traces do not know the pipeline name.

**Proposal.** `run(pipeline="split_scenes", input=..., params=...)` as an
alternative to `spec` (exactly one of them). The trace and the board record
the pipeline name. `get_pipeline` stays for reading and editing.

**Tests.** `run` with a pipeline name equals `run` with its spec; both
`spec` and `pipeline` (or neither) is an error; an unknown name lists the
saved ones.

### 5. Pipeline parameters (`$param`) [ico, mcp]

**Status.** Done: apriori3d/ico#36, ico-mcp#1.

**Evidence.** Everything except the input is fixed in a saved spec:
`out_dir`, the ffmpeg binaries, detector thresholds. To reuse
`split_scenes` for several videos, the operators themselves had to derive
per-video directories (`<out_dir>/<video slug>/`). Trying another
threshold means saving another pipeline. The input is also the only way to
pass per-run data, and it is a single JSON value.

**Proposal.** A param value may be `{"$param": "name", "default": ...}`.
`run(..., params={"out_dir": "/tmp/x"})` substitutes values before
building; a parameter without a default and without a value is a
`spec_error`. `validate` reports the declared parameters with their
types (inferred from the operator param types); `get_pipeline` and
`list_pipelines` show them. Substitution lives in `ico.spec` (the same for
a future CLI); the server only passes `params`.

**Tests.** ico: substitution, defaults, a missing parameter, a parameter
used by two nodes with different types (error). mcp: `save_pipeline` keeps
`$param` nodes; `run(pipeline=..., params=...)`.

### 6. Run titles ignore the spec `name` [mcp]

**Status.** Done: ico-mcp#1 (pipeline or root name and params); the input in the title is not shown.

**Evidence.** Every run on the Live Board and in `run_status` is titled by
the sorted list of operator refs (`cut_clip@1, detect_scenes@1,
load_video@1, scene_cuts@1, scene_report@1`;
`jobs.py: ", ".join(traces.op_refs(spec))`). Three runs of `split_scenes`
on different videos and with different parameters looked the same; the
spec had `"name": "split_scenes"`.

**Proposal.** Title = pipeline name (item 4), else the root node `name`,
else the op refs as now; plus a subtitle with the input when it is a short
string (e.g. the file name) and non-default `params`.

**Tests.** The title for a named root, a saved pipeline and an unnamed spec.

## Environment and lifecycle

### 7. Environment report and binary capabilities [mcp]

**Status.** Done: ico-mcp#5.

**Evidence.** `cut_clip` failed with `ffmpeg exited with 8` because the
first `ffmpeg` on the kernel's `PATH` (miniconda) has no libx264;
`/opt/homebrew/bin/ffmpeg` has. The agent found this only by reproducing
the command in its own shell. It also could not see which Python packages
the kernel has before writing an operator (it learnt that `cv2` and
`scenedetect` exist only after `uv add`). `caps` are free strings and
nothing checks them.

**Proposal.**
- An `environment` tool: Python version, the kernel's `PATH`, the
  installed distributions with versions, the `env` allowlist, timeouts.
- A convention for `caps`: `bin:<name>` means a binary on the kernel's
  `PATH`. `validate` and `run` resolve the `bin:` caps of the operators in
  the spec and report a missing binary as an issue with its path lookup;
  the card shows where each binary resolves.
- [guide] Recommend making binaries operator params (as `cut_clip`
  does: `ffmpeg="ffmpeg"`), so a spec can pin one.

Checking codec support (libx264) stays the operator's job.

**Tests.** `environment` lists the test packages; a spec with an operator
whose `bin:` cap is missing gets a validate issue.

### 7a. Domain dependencies that clash in one process [mcp, guide]

**Status.** Done: ico-mcp#5 (duplicates in `environment`, guide); the network check of the session assessment is open.

**Evidence.** (1) `uv add paddleocr` pulled `opencv-contrib-python` 4.10
next to `opencv-python` / `opencv-python-headless` 5.0 (from
scenedetect); they share the `cv2` package directory, and the last
installed (4.10) silently won. (2) The kernel and every worker import all
modules of the draft package, so torch (TransNetV2) and Paddle ended up in
one process; on macOS the two OpenMP runtimes crash together (item 0).
PaddleX itself imports torch through modelscope when torch is installed.
The agent worked around it by importing torch lazily and blocking it in
the OCR process (`sys.modules["torch"] = None`).

**Proposal.** [mcp] The `environment` tool (item 7) reports duplicate
distributions that own the same top-level package (`cv2` from three
OpenCV wheels). [guide] Section 4: import heavy frameworks inside the
operator (on first use), never at module level, since every module is
imported in every kernel and worker; keep operators that need clashing
frameworks in different workers.

### 8. Stale tool list after a server update is invisible to the agent [mcp]

**Status.** Done: ico-mcp#2 (`server_status`, fingerprint of the server code).

**Evidence.** The desktop app had started the server before the commits
that added `guide`, `propose_types`, `run_status` and the rest; the
session saw 9 tools, while the instructions mentioned `guide`. After the
process was restarted the session still had the old tool list and the old
`run` schema (without `wait`) until the app was restarted. The agent could
not tell that it was talking to an old server.

**Proposal.** Report the server version (package version plus git commit
and a dirty flag) in the server `instructions` header and in `reload` /
`dashboard` results, and the tool names the server provides. An agent that
sees a tool named in the instructions missing from its list can then say
"the client has a stale tool list; reconnect the server". Send
`notifications/tools/list_changed` after `reload` (harmless if the client
ignores it).

**Tests.** The version string in the instructions and in `reload`.

### 9. Small edits of a draft module need the whole module [mcp]

**Status.** Done: ico-mcp#3.

**Evidence.** `scenes.py` grew to about 300 lines; each fix (a missing
`ffprobe` param, stderr in errors, a label position) meant sending the
whole module again through `propose_operator`. Once the agent edited the
file on disk instead, which bypasses the proposal stages until the next
`reload`.

**Proposal.** `propose_operator(module, edits=[{"old": ..., "new": ...}])`
as an alternative to `code`: exact-match replacements applied to the
current draft file (each `old` must match once), then the same stages.
The same for `propose_types`. Keep `code` for new modules and rewrites.

**Tests.** An edit applied and staged; a non-unique or missing `old` is a
`write` error that names the edit; an edit of a missing module is an
error.

### 10. Replacing a draft silently changes the meaning of runs and pipelines [mcp]

**Status.** In review: ico-mcp#6.

**Evidence.** `detect_scenes@1` was replaced three times while staying
`@1`; once `min_scene_len` changed from seconds to frames. Run traces and
the board refer to `detect_scenes@1` for all of them, so earlier results
cannot be told apart. The guide says to bump versions only for stable
operators, which is right for drafts in flux, but nothing records which
draft code a run used.

**Proposal.** Fingerprint drafts: the hash of the module source, shown on
draft cards and stored in run traces next to each ref. `propose_operator`
warns when a replaced operator's signature or params change while a saved
pipeline uses it, and `validate` of saved pipelines runs as part of the
proposal (fail only on errors).

**Tests.** The trace of a run carries the hash; a param change of an
operator used by a saved pipeline gives a warning that names the pipeline.

### 11. Examples cannot test operators on domain types [mcp, guide]

**Status.** Done, revised: ico-mcp#4 (examples stay for pure operators; the crystallize PR shows recent runs as evidence; no spec examples).

**Evidence.** The agent gave an example for `load_video@1` (`str →
VideoRef`); the proposal failed at `examples` with `output of type VideoRef
is not JSON-compatible`. Examples run the bare operator through the JSON
boundary, so they only fit JSON → JSON operators, and most domain
operators get no tests on `crystallize`.

**Proposal.**
- [mcp] A clearer `examples` error: "examples need a JSON-compatible
  output; this operator returns VideoRef; test it with a spec example".
- [mcp] Spec examples: `{"spec": ..., "input": ..., "output": ...}`
  (e.g. `load_video → video_summary`) checked like operator examples and
  saved with the module; they become tests on `crystallize` too. Outputs
  may be compared on a subset of keys (`"match": "subset"`) so that paths
  and timings do not make them brittle.
- [mcp] Optionally: when an output is a dataclass, compare its
  `dataclasses.asdict` form, which covers the simple `str → VideoRef` case.

**Tests.** A spec example passes and fails as expected; a dataclass output
compared as a dict.

### 12. Progress inside a long operator [ico, mcp]

**Status.** Open (low; deferred until a real long operator needs it).

**Evidence.** `detect_scenes` decodes the whole video (seconds now, minutes
for long videos; neural cleaning per clip will take far longer) with no
progress: the board shows the run as started until the first clip is cut.
Progress nodes count only items between operators.

**Proposal.** [ico] A progress API usable from inside an operator, e.g.
`self.report(done, total, message)` on `IcoOperator` (a no-op without a
progress tool), shown on the board as a progress row of that node. [mcp]
Nothing beyond forwarding it.

**Tests.** ico: an operator reporting progress produces snapshots. mcp:
the board row of that node.

### 13. Delete a saved pipeline [mcp]

**Status.** Done: ico-mcp#2.

**Evidence.** Pipelines can be saved and replaced, not removed; research
pipelines (`score adaptive 2.0 w1`) would pile up.

**Proposal.** `delete_pipeline(name)`: a plain delete of the file. (A
pipeline already in a crystallization branch stays in that branch; no
special handling until it matters.)

**Tests.** Delete, then `list_pipelines` without it; an unknown name.

### 14. This repository is used as a domain directly [process]

**Status.** In progress: the domain work moved to the branch `feat/ico-mcp-clean-video`; the domain clone is still to be created.

**Evidence.** The session added `scenedetect` and
`opencv-python-headless` to this repository's `pyproject.toml` and wrote
its drafts into `domain_draft/` here, because the template had not been
cloned into a video domain server. `scenedetect` also pulled
`opencv-python` next to `opencv-python-headless` (two OpenCV builds).

**Proposal.** Create the domain clone (e.g. `video-mcp`, package
`video`) as described in the README, move the drafts, the saved pipeline
and the dependencies there, and revert `pyproject.toml` / `uv.lock` here.
Pin `opencv-python-headless` only and exclude `opencv-python`
(`[tool.uv] override-dependencies`), or accept the GUI build.

## Guide changes collected

**Status.** All in the guide (ico-mcp#1, #4, #5), except "spec examples"
(item 11 was revised: examples stay for pure operators).

- Section 4: `from ico import IcoOperator` (item 1).
- Section 1: "pass a string input as is, without quotes" (item 2).
- Section 2: `progress.total` optional once item 3 lands; until then
  "required; pass the expected count".
- Section 4: binaries as operator params, `bin:` caps (item 7);
  a `_run` helper pattern that raises with the command's stderr (the
  first ffmpeg failure showed only an exit code).
- Section 4: examples only for JSON → JSON operators, spec examples for
  the rest (item 11).
- Section 6: `run(pipeline=..., params=...)` (items 4, 5).

## Not included

Findings that belong to the domain, not to the server: scene detection on
videos with variable frame rate (use `r_frame_rate`), duplicated frames
from 25 → 30 fps conversion causing false cuts, frame-exact cutting with
`trim`. They go into section 8 of the guide of the domain clone.

## Session assessment (agent's view)

Written by the agent at the end of the session, after building scene
splitting, text detection, masks and ComfyUI cleaning on 7 videos.

### What worked

- **Typed links and `validate`** caught mismatches before costly runs
  (`list` vs `Iterator`, `str` vs a domain type, progress params).
- **Composition** made the decisive experiments cheap: three detectors in a
  `fork` with `merge` and `score_scenes_dir` against references; a
  `filter` for user drops; an edits operator between detection and cutting.
  None of these needed new code in existing operators.
- **Parallelism** (`async_stream` + `MPWorker`) and the **Live Board** came
  almost for free; run titles with pipeline name and params tell runs apart.
- **Pipeline parameters and run by name** turned saved pipelines from
  copied JSON into reusable tools: a rerun is a few hundred tokens.
- **Fast server iteration**: proposal items landed the same day.

### What cost most (ranked)

1. **Whole-module proposals** for small edits (item 9): a ~300-line module
   was resent about six times; twice the agent edited the file directly,
   bypassing the stages.
2. **Silent hangs** when a worker process crashed (item 0): 10 minutes
   lost, cause found only in a macOS crash report.
3. **An opaque kernel environment** (items 7, 7a): first `ffmpeg` on
   `PATH` without libx264; torch and Paddle crashing together; three OpenCV
   wheels sharing `cv2`; the kernel (started through Claude.app's
   `disclaimer`, so macOS treats its python as the responsible process)
   needed its own Local Network permission to reach a LAN ComfyUI while the
   agent's shell could. Each took a separate investigation. All draft
   modules being imported in every worker spreads heavy imports and
   clashes to every pipeline.
4. **Stale client tool schemas** after server updates (item 8): new
   arguments (`params`, `pipeline`) were silently dropped until the app
   was restarted.
5. **The JSON boundary for domain types** (item 11): a loader and a summary
   operator per type, and no examples, hence no tests for most operators.
6. **Waiting for long runs**: the agent polls `run_status` with sleep
   loops; a `wait_run(run_id, timeout)` or a completion notification would
   help.

### Economics: ico vs plain code vs skills

Estimates from this session, not measurements.

- **First pass** (exploration, first video): ico cost roughly 1.5-2x the
  tokens of throwaway scripts — operators, types, specs, environment
  debugging, whole-module resends.
- **Every further run** (more videos, other thresholds, reruns after
  edits): near zero code tokens; `run(pipeline=..., params=...)` plus
  checking results. Break-even after two or three reuses; for 15 videos
  with quality iterations ico is clearly cheaper.
- **Context**: operators appear as compact cards, data crosses by path, so
  the agent's context holds signatures, not code.
- **Skills** store judgment and procedure ("use r_frame_rate on VFR",
  "never Paddle and torch in one process", "ProPainter caps at 960 px,
  composite by the mask") cheaply, but the code is regenerated or copied
  each time and nothing validates or orchestrates it. They complement ico:
  operators for executable steps, the guide's domain notes (or a skill)
  for when and how to use them.
- **Working pattern that emerged**: investigate in scratch scripts
  (frame checks, crash analysis), then turn every settled step into an
  operator and a parametrized pipeline at once.

### Top three to cut the first-pass cost

**Status.** 1 and 2 done (ico-mcp#3, apriori3d/ico#37); 3 done except the
network reachability check (see the follow-ups below).

1. `propose_operator(module, edits=[...])` (item 9).
2. Dead-worker detection in `MPWorker` (item 0).
3. An `environment` tool that checks from inside the kernel: packages,
   `PATH` binaries of `bin:` caps, and network reachability of configured
   hosts (items 7, 7a).

## Follow-ups not yet itemized

Found in the session assessment above; not done, candidates for the next
proposal:

- **Wait for a run** (cost 6): the agent polls `run_status` with sleep
  loops; a `wait_run(run_id, timeout)` that returns when the run ends or
  the timeout passes.
- **Network reachability** (top three, 3): `environment` checks, from the
  kernel, that configured hosts (e.g. a LAN ComfyUI) are reachable. On
  macOS the kernel, started through the desktop app, needed its own
  Local Network permission while the agent's shell did not: document it.
- **Every draft module in every process** (cost 3): the kernel and every
  worker import all draft modules, which spreads heavy imports and
  framework clashes to every pipeline; importing only the modules a spec
  uses would contain them.
- **The input in the run title** (item 6, the part not done).


541:## Addendum 2026-10-18: GPU on cube (split_scenes_batch, 7 videos, pool 4)
569:## Addendum 2026-10-18 (2): after the update to 013ca66
## Addendum 2026-10-18: GPU on cube (split_scenes_batch, 7 videos, pool 4)

| variant | time | scenes |
|---|---|---|
| TransNet CPU + x264 slow | 168 s | 31/32/5/27/5/2/27 |
| TransNet CUDA + x264 slow | 80 s | same |
| TransNet CUDA + NVENC p5 cq14 | 77 s | same |

NVENC saves ~3 s per video (x264 slow 4.9 s vs 1.9 s for a 30 s master),
PSNR to source 52.5 dB vs 50.5 dB, files 1.6× larger. Without per-operator
timing it is guesswork where the remaining ~75 s go (adaptive detector in
Python, ffprobe -count_frames per clip, strips, worker startup with CUDA).

New findings:

1. **run(wait=true) longer than the MCP request timeout kills the kernel.**
   The client saw "Request timed out"; the run was lost halfway (video 07
   unfinished), the kernel came back on a new board port. Expected: the call
   turns into a background run (return run_id) instead of dying; the board
   port should stay fixed (proposal: configurable host/port).
2. **The first run after that restart hung forever**: 3 of 4 MPWorkers spawned,
   kernel and workers all blocked in pipe_read, no progress nodes on the
   board. cancel_run worked; the identical rerun was fine. Needs a spawn
   timeout / worker health check in async_stream.
3. **run input as a JSON string**: `input="\"/path\""` reached the operator
   with the quotes kept (`no directory at <cwd>/"/path"`). Either decode a
   JSON-looking string or say in the tool schema that input is raw.

## Addendum 2026-10-18 (2): after the update to 013ca66

4. **wait_run longer than the client's request timeout kills the server and
   the run.** The Claude app times a tool call out after about 60 s;
   wait_run(timeout=600) and wait_run(timeout=120) both ended in "Request
   timed out", the app restarted the server, and the kernel with the run was
   gone (run_status: not_found). wait_run(45) works. Cap `timeout` below the
   client limit (or make it configurable in ico.toml) and say so in the tool
   description; a client disconnect should not kill a background run.
5. **Every server restart moves the board to a new port** (34427 → 44001 →
   34933 → 44297 → 34821 in one session), so a LAN proxy has to be
   restarted each time. A fixed `board.port` / `board.host` in ico.toml
   would solve it.
6. **environment(match=...) is a substring, not a pattern**: `torch|diffusers`
   returned no packages. Fine, but say so in the description, or accept a list.
7. environment found three OpenCV distributions sharing `cv2`
   (opencv-contrib-python 4.10 from PaddleOCR, opencv-python and -headless
   5.0). Good catch; the tool could also say which one wins.

## Addendum 2026-10-18 (3): profiler (ico 035eb23, ico-mcp a473195)

8. **The profiler works and answers the right questions.** On
   split_scenes_batch it showed that cut_clip took 43% of all work, that
   NVENC costs ~0.5 s of session start per clip (slower than x264 on short
   clips), and that the pool idled 27% (worker start ~8 s each, plus the
   tail of 7 videos on 4 slots). Self vs CPU tells at once that Python only
   waits for ffmpeg. Two readings were misleading:
   - `startup` of an operator called once per item (normalize_video, 7
     calls on 4 workers) is the first 4 items, not setup; show it only when
     the first call is far above p95, or say "first call".
   - `run` does not list `profile` in its tool schema (the client still
     shows the old schema after an app restart), yet the server accepts it.
9. **profile=true breaks imports inside an MPWorker.** vace_scenes with
   profile=true: importing diffusers in the worker failed in scipy's
   docscrape (`self._l >= len(self._str)`: `'>=' not supported between
   instances of 'builtin_function_or_method' and 'int'`), i.e. `self._l`
   or `len` was replaced by a function while the profiler was active. The
   same run without profile works. Probably the probe wraps something it
   should not (a builtin or a class attribute) during the first call.
10. **One item error leaves an MPWorker unusable.** After the error above,
    every later item of the same async_stream (on_error: collect) failed at
    once with `Cannot transition to Sending state from non-Ready state`, so
    28 items reported errors for one. Expected: the worker goes back to
    Ready after an item error (or is restarted), and the other items run.
11. **Two async_streams in a row fail.** seq [..., async_stream(pool 1,
    worker: vace_generate), async_stream(pool 2, worker: composite + write),
    ...] stops at once with `RuntimeError: Cannot run the event loop while
    another loop is running` (async_to_sync_iter of the first stream runs
    `loop.run_until_complete` inside the second stream's running loop).
    A GPU stage and a CPU stage overlapping is the natural shape for model
    pipelines; workaround used: the GPU stage as a plain `stream` over one
    `worker`, only the CPU stage as an async_stream. Either support
    chaining (one loop, or a thread per bridge) or reject it in `validate`.
12. With the decomposed pipeline (vace_plan → vace_generate → composite_clean
    → write_clip) the profiler ran fine inside workers, and the result was
    bit-identical to the single operator. So item 9 (import failure under
    profile) is specific to something in the first layout (an async_stream
    with factory over an MPWorker whose first call imports diffusers).
13. **An async_stream fed by a slow synchronous stream shows no progress.**
    vace_scenes_steps: the CPU async_stream (pool 2) takes items from a
    plain stream over the GPU worker (~3-5 min per item). The CPU workers do
    run (cleaned clips appear one scene behind the GPU), but the progress
    node after the async_stream stays at 0/28 for the whole run: outputs
    are not yielded while the scheduler is blocked pulling the next input.
    Pulling input in a thread (or yielding finished outputs before the next
    blocking pull) would fix both the progress and the one-scene lag.
14. **progress total null.** A progress node with `total: {"$param": "total",
    "default": null}` validates only when check params give `total`; with
    no value, `validate`/`save_pipeline` fail with `Param 'total' of
    'progress' must be a number, got None`, although null is the documented
    "unknown". Treat null as "no total".
15. With every step its own operator (vace_chain: split_windows →
    vace_window → group_windows → crossfade_windows → color_fix → edge_fix →
    feather_composite → write_clip) the profiler shows the cost of each:
    color_fix 7.5 s, edge_fix 1.5 s, crossfade 0.7 s per scene against
    ~90 s per window. A progress node inside the workers (after vace_window,
    after write_clip) counts real completions; one after the async_stream
    does not (item 13).
16. **A saved pipeline as a node of another pipeline.** clean_video (one
    video end to end: OCR → refine_masks → VACE chain → assemble) repeats
    the specs of scan_text_batch, refine_masks, vace_chain and
    assemble_video by copy: a fix in one of them has to be made twice, and
    their descriptions and check params drift apart. Proposal: a node kind
    `{"kind": "pipeline_ref", "pipeline": "vace_chain", "params": {...}}`
    that inlines a saved pipeline at build time (its `$param`s bound from
    the outer pipeline or fixed), validated at its links like any node, and
    shown on the dashboard as a collapsible group with the inner pipeline's
    name. `save_pipeline` would record the referenced pipelines, and
    `delete_pipeline` / a replaced pipeline would report the pipelines that
    use it (like draft `changes`).
17. **Steps that hand over through files.** Whole-video steps here return a
    report (dict) and write their real result next to the master
    (clean.json, masks, cleaned clips); the next step needs the master
    again. Today that is an adapter operator (`master_of: object →
    VideoRef`, reading the report's manifest path). Alternatives to
    consider: a `tap` node (run a child for its side effect, pass the input
    through: `master → tap(scan) → master → tap(refine) → ...`), or a
    convention that such steps return a typed result that carries the
    input ref. A `tap` would also remove the need for `object` inputs.
18. **Item 13 is worse with a grouping step in between.** vace_chain:
    stream(gpu worker: vace_window) → group_windows (generator, one item
    per scene after its windows) → async_stream(cpu pool 2). After 15 min
    and 10 of 77 windows (3 scenes complete) the CPU workers were alive and
    idle and had received nothing: the async_stream does not dispatch while
    its scheduler is blocked pulling the next input from a slow synchronous
    upstream. The CPU work then runs at the end in one burst, and the
    progress inside the CPU workers stays at 0 meanwhile. A thread for the
    input pull (or dispatching each item before the next pull) is needed
    for GPU → CPU pipelines.
19. **A filter loses the stream's length.** load_scene_clips returns
    iter(list) (32 scenes), but after `filter(keep_scene)` the progress node
    has no total: the filter yields kept items one by one. Workaround: an
    operator `drop_scenes: Iterator[SceneClip] → Iterator[SceneClip]` that
    returns iter(list) of the kept ones (28). Option for ICO: when the
    filter's input has a length, evaluate the predicate over all items and
    return a list (predicates are cheap by convention), or pass a length
    hint after a first pass; same question for `partition`.
20. **A length hint for reducing operators.** group_windows (77 windows →
    28 scenes) knows the number of its outputs from its inputs' metadata
    before consuming them, but an iterator cannot say so without being
    consumed. A protocol (`__length_hint__`, which Python already defines
    for iterators) read by stream/progress would give totals to reducers
    too.
21. **sample_run, first use (ico c926a5f, ico-mcp 231c7d8).** On clean_video:
    - Works on nodes of the run's own process: color_fix gave two
      SceneLayers previews with the array shape and the steps so far.
    - Gives nothing for nodes inside a `worker` (scan_text, text_masks,
      text_report in the OCR worker): `samples: []`, `complete: false`
      after waiting, while several scenes passed through. Either sample in
      workers too (the probe is already there for the profiler) or say in
      the result that the node runs in a worker process.
    - Only future results: vace_plan returned nothing because all its items
      had passed before the call. A small ring buffer of the last N results
      per node would let an agent look at what already happened.
    - The preview is a truncated repr (head ... tail); for dataclasses with
      arrays the middle (the scene index) is cut away. A preview hook
      (e.g. `__ico_preview__` on the type, or a field-wise summary: scalars
      in full, arrays as shape/dtype/min/max) would make it readable.
22. **sample_run in workers (ico 0c546b3, ico-mcp dd33658): works.** scan_text
    inside the OCR worker of scan_text_batch gave two TextScan previews
    (the last mask line: text, score, box; review empty), i.e. what OCR read
    before masks are built. Items 21's other points stand: only future
    results, and the repr cut in the middle.
23. **A stream does not pass its input's length on.** load_clean_tasks
    returns iter(list) (28), then `stream(vace_plan)` (one output per
    input), then `stream(seq[diffueraser_generate, progress])`: the progress
    has no total. A 1:1 stream could wrap its output iterator with the
    input's length (`__length_hint__`), so totals survive a chain of maps.
24. **No way to remove a draft module or operator.** Refactoring vace_* into
    generic `cleaning` (plan_clean, load_layers, color_fix, edge_fix,
    feather_composite, write_clip) + `vace_chain` + `diffueraser` needed
    removing vace_steps, vace_clean and vace_types; propose_operator only
    adds or replaces, and a new module cannot register an operator name an
    old one still holds. Done by deleting the files over ssh and `reload`.
    A `delete_module(module)` (reporting the saved pipelines that use its
    operators, like `changes`) would keep this inside the tools.
