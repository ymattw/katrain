# Lazy hover PV refinement

Design document for lazy hover principal variation (PV) refinement and the
KaTrain-side bulk analysis scheduler.

## 1. Summary

- When hovering over a candidate move or child move in analysis mode, KaTrain
  picks the longest PV available at that coordinate (`on_mouse_pos`).
- If the PV has fewer than `HOVER_PV_MOVES` (10) moves and has not yet been
  refined on the current node, `BadukPanWidget` pauses new bulk analysis
  submissions, waits for in-flight bulk queries to drain, and requests a
  streaming `next_move` refinement query with `PRIORITY_ALTERNATIVES`.
- Bulk full-game analysis (`analyze_all_nodes` and `analyze_extra("game")`) is
  scheduled through a KaTrain-side queue in `Game` capped at `_analysis_limit`
  in-flight queries, rather than dumping the entire game tree into KataGo at
  once.
- Once the hover query finishes (or the cursor leaves), bulk analysis resumes
  after `HOVER_RESUME_DELAY` (2.0s) without clearing already-completed nodes.

## 2. Problem

1. **Shallow candidate PVs**: Candidate move PVs come from the current node's
   root analysis (`analysis["moves"][gtp]["pv"]`), which often only reaches
   1–2 moves for non-top moves, whereas played child moves have deeper PVs from
   full-game analysis.
2. **Overlapping entries at the same coordinate**: `active_pv_moves` can contain
   both a candidate hint entry and a child-move entry at the same coordinate.
   Previously `on_mouse_pos` picked `near_move[0]`, often showing the shorter
   root-analysis PV instead of the deeper child PV.
3. **Engine starvation during full-game analysis**:
   - `analyze_all_nodes` previously submitted every node in the tree to
     `engine.write_queue` in a loop.
   - In KataGo, `priority` only orders queued queries and cannot preempt
     in-flight searches.
   - `terminate` is asynchronous, and `engine.queries_remaining()` drops as
     soon as KaTrain removes its tracking entry, even while KataGo is still
     finishing the search on the GPU.
   - Reducing `numAnalysisThreads` globally slows down full-game throughput on
     large models.

## 3. Architecture

### 3.1 KaTrain-side bulk analysis scheduler (`katrain/core/game.py`)

`Game` maintains its own thread-safe queue and in-flight tracking under
`self._analysis_lock`:

- `_analysis_queue`: pending node items (`node`, `priority`, `analyze_fast`,
  `visits`, `time_limit`, `report_every`).
- `_analysis_inflight` / `_analysis_inflight_items`: currently submitted tokens
  awaiting final completion (`partial_result == False` or `error_callback`).
- `_analysis_limit`: max concurrent bulk queries, defaulting to
  `engine.analysis_threads` (or `4` if unavailable), overridable via
  `engine/analysis_limit`.

Key behaviors:

- **Controlled pumping (`_pump_analysis`)**: Submits items only while
  `not _analysis_paused` and `len(_analysis_inflight) < _analysis_limit`.
- **Navigation priority (`set_current_node` / `_pop_next_analysis_item`)**:
  Skips already-completed nodes and promotes `current_node` and
  `current_node.parent` ahead of older queued nodes.
- **Pause & drain (`pause_analysis`, `set_on_analysis_drained`)**:
  - Default (`terminate=False`): stops submitting new bulk queries while letting
    the few in-flight queries finish naturally (typically a few seconds), then
    fires the drain callback.
  - Optional (`terminate=True` or `engine/hover_terminate_inflight = true`):
    terminates in-flight bulk queries immediately and re-inserts unfinished
    nodes at the front of `_analysis_queue`.
- **Resume (`resume_analysis`)**: Clears `_analysis_paused` and calls
  `_pump_analysis()` without calling `clear_analysis()` on already-completed
  nodes.
- **Stop (`analyze_extra("stop")`)**: Clears `_analysis_queue`,
  `_analysis_inflight`, and `_on_analysis_drained`.
- **Stale token pruning (`_prune_stale_inflight`)**: Drops entries older than
  `0.5s` if all engines report `is_idle()` (for example after an engine restart).

### 3.2 Lazy hover PV refinement (`katrain/gui/badukpan.py`)

Constants in `katrain/gui/badukpan.py`:

| Constant              | Value | Purpose                                        |
| --------------------- | ----- | ---------------------------------------------- |
| `HOVER_PV_MOVES`      | `10`  | Target PV length below which refine triggers   |
| `HOVER_PV_DEBOUNCE`   | `0.35`| Seconds to wait before sending refine query    |
| `HOVER_RESUME_DELAY`  | `2.0` | Seconds after hover ends before resuming bulk  |
| `HOVER_TIMEOUT_DELAY` | `15.0`| Fallback timeout to reset state & resume bulk  |

Lifecycle:

1. **Selection (`on_mouse_pos`, `set_animating_pv`)**:
   - Picks `max(near_move, key=lambda item: len(item[0]))` when multiple entries
     exist at the hovered coordinate.
   - If the user stays on the same move (`node` and `pv[0]` unchanged) and the
     PV grows, `animating_pv` updates in place while preserving `start_time` so
     the animation does not restart.
2. **Scheduling (`_maybe_schedule_refine`)**:
   - Skips moves with `len(pv) >= HOVER_PV_MOVES`, pass moves, or moves already
     recorded in `node._hover_pv_requested` (reset on `GameNode.clear_analysis()`).
   - Cancels any pending resume trigger and calls `game.pause_analysis()`
     immediately so in-flight bulk searches start draining during the `0.35s`
     debounce window.
3. **Draining & Requesting (`_refine_hover_pv`)**:
   - If `game.analysis_inflight_count > 0`, registers a drain callback via
     `game.set_on_analysis_drained(...)` to re-invoke `_refine_hover_pv` on the
     Kivy main thread once KataGo is idle.
   - Sends a `next_move` query with `PRIORITY_ALTERNATIVES`, `time_limit=False`,
     `report_every=PONDERING_REPORT_DT`, and
     `visits = max(engine/max_visits, cur_visits + engine/fast_visits)`.
4. **Streaming & Completion (`_on_hover_refined`, `_finish_hover_query`)**:
   - Calls `node.set_analysis(..., refine_move=move, partial_result=...)` and
     keeps `node.analysis["moves"][gtp]["pv"]` updated if `new_pv` is longer
     than the existing entry.
   - Updates `animating_pv` live on partial and final responses and triggers a
     hover redraw.
   - Resets `hover_timeout_trigger` on partial results; on completion or error,
     decrements `_hover_in_flight` and schedules `hover_resume_trigger` (or
     immediately refines the currently hovered move if the cursor moved to
     another shallow candidate).

### 3.3 Engine and UI integration

- `katrain/core/engine.py`: `KataGoEngine.analysis_threads` tracks the active
  `numAnalysisThreads` setting (explicit `engine/num_analysis_threads`,
  `auto_thread_settings()`, or `12` default).
- `katrain/core/engine.py` & `katrain/core/remote_engine.py`: invoke
  `error_callback(analysis)` on completed `noResults: true` responses so
  terminated queries always release scheduler/hover slots.
- `katrain/__main__.py`:
  - `update_gui` includes `game.queued_analyses` in the status dot idle check
    and `queries_remaining` counter.
  - `_do_update_state` avoids starting pondering while `game.analysis_paused` is
    `True`.

## 4. Configuration and debugging

- `engine/analysis_limit` (optional): overrides the KaTrain-side bulk
  concurrency limit.
- `engine/num_analysis_threads` (optional): overrides `numAnalysisThreads`
  passed to KataGo.
- `engine/hover_terminate_inflight` (optional, default `false`): when `true`,
  hovering terminates in-flight bulk queries instead of waiting for them to
  drain.
- Debug logging (`general/debug_level`):
  - `>= 1` (`OUTPUT_DEBUG`): logs `[hover-pv]` schedule, drain wait, request,
    completion, error, and timeout events.
  - `>= 2` (`OUTPUT_EXTRA_DEBUG`): logs `[hover-pv]` skip reasons, overlapping
    entry selection, and partial PV updates.

## 5. Tests

Unit tests in `tests/test_hover_pv.py` cover:

- Concurrency limiting (`_analysis_limit`) and sequential pumping on completion
  and `noResults` errors.
- `pause_analysis(terminate=False)` draining, `set_on_analysis_drained`
  notification, and `resume_analysis()` preserving completed nodes.
- `set_current_node` prioritization of `current_node` and `current_node.parent`.
- `pause_analysis(terminate=True)` terminating in-flight nodes and re-queueing
  unfinished items at the front of `_analysis_queue`.
- `GameNode.clear_analysis()` resetting `_hover_pv_requested`.
