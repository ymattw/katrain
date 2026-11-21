"""KaTrain-side bulk analysis queue and concurrency scheduler."""

import threading
import time
from typing import Dict, List, Optional, Tuple

from katrain.core.constants import PRIORITY_GAME_ANALYSIS, REPORT_DT
from katrain.core.game_node import GameNode


class BulkAnalysisScheduler:
    """Schedules bulk game-tree analysis with a concurrency cap and pause/drain support."""

    def __init__(self, game):
        self._game = game
        self._lock = threading.RLock()
        self._queue: List[Dict] = []
        self._inflight: Dict[object, Tuple[GameNode, float]] = {}
        self._inflight_items: Dict[object, Dict] = {}
        self._paused = False
        self._on_drained = None
        first_engine = next(iter(game.engines.values()), None)
        default_limit = getattr(first_engine, "analysis_threads", None) or 4
        self._limit = max(1, int(game.katrain.config("engine/analysis_limit") or default_limit))

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def inflight_count(self) -> int:
        with self._lock:
            self._prune_stale_inflight()
            return len(self._inflight)

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def set_on_drained(self, callback):
        call_now = False
        with self._lock:
            self._prune_stale_inflight()
            if self._inflight:
                self._on_drained = callback
            else:
                self._on_drained = None
                call_now = callback is not None
        if call_now:
            callback()

    def clear(self):
        with self._lock:
            self._queue.clear()
            self._inflight.clear()
            self._inflight_items.clear()
            self._on_drained = None
            self._paused = False

    def enqueue(
        self,
        node: GameNode,
        priority=PRIORITY_GAME_ANALYSIS,
        analyze_fast=False,
        visits=None,
        time_limit=True,
        report_every=REPORT_DT,
    ):
        item = {
            "node": node,
            "priority": priority,
            "analyze_fast": analyze_fast,
            "visits": visits,
            "time_limit": time_limit,
            "report_every": report_every,
        }
        with self._lock:
            self._queue = [q for q in self._queue if q["node"] is not node]
            self._queue.append(item)

    def pump(self):
        to_submit = []
        with self._lock:
            self._prune_stale_inflight()
            while not self._paused and len(self._inflight) < self._limit:
                item = self._pop_next()
                if item is None:
                    break
                token = object()
                self._inflight[token] = (item["node"], time.time())
                self._inflight_items[token] = item
                to_submit.append((token, item))
        for token, item in to_submit:
            self._submit(token, item)

    def pause(self, terminate: Optional[bool] = None) -> bool:
        if terminate is None:
            terminate = bool(self._game.katrain.config("engine/hover_terminate_inflight", False))
        to_terminate: List[GameNode] = []
        drain_cb = None
        with self._lock:
            was_paused = self._paused
            self._paused = True
            if terminate and self._inflight:
                requeue = []
                queued_nodes = {q["node"] for q in self._queue}
                for tok, (node, _ts) in self._inflight.items():
                    to_terminate.append(node)
                    item = self._inflight_items.get(tok)
                    if item and not self._is_done(item) and node not in queued_nodes:
                        requeue.append(item)
                        queued_nodes.add(node)
                if requeue:
                    self._queue = requeue + self._queue
                self._inflight.clear()
                self._inflight_items.clear()
                if self._on_drained:
                    drain_cb = self._on_drained
                    self._on_drained = None
        for engine in set(self._game.engines.values()):
            if hasattr(engine, "stop_pondering"):
                engine.stop_pondering()
        for node in to_terminate:
            engine = self._game.engines.get(node.next_player)
            if engine and hasattr(engine, "terminate_queries"):
                engine.terminate_queries(only_for_node=node)
        if drain_cb:
            drain_cb()
        return not was_paused

    def resume(self):
        with self._lock:
            self._paused = False
            self._on_drained = None
        self.pump()
        if getattr(self._game.katrain, "update_state", None):
            self._game.katrain.update_state()

    def _prune_stale_inflight(self):
        if not self._inflight:
            return
        now = time.time()
        all_idle = all(
            (not hasattr(e, "queries") or not hasattr(e, "write_queue")) or e.is_idle()
            for e in set(self._game.engines.values())
        )
        if not all_idle:
            return
        stale = [tok for tok, (_node, ts) in self._inflight.items() if now - ts > 0.5]
        for tok in stale:
            self._inflight.pop(tok, None)
            self._inflight_items.pop(tok, None)

    @staticmethod
    def _is_done(item: Dict) -> bool:
        node = item["node"]
        return node.analysis_complete and (item["visits"] is None or node.root_visits >= item["visits"])

    def _pop_next(self) -> Optional[Dict]:
        with self._lock:
            while self._queue and self._is_done(self._queue[0]):
                self._queue.pop(0)
            if not self._queue:
                return None
            current = self._game.current_node
            priority_nodes = [n for n in (current, getattr(current, "parent", None)) if n is not None]
            for target in priority_nodes:
                for idx, item in enumerate(self._queue):
                    if item["node"] is target:
                        item = self._queue.pop(idx)
                        if not self._is_done(item):
                            return item
                        break
            while self._queue:
                item = self._queue.pop(0)
                if not self._is_done(item):
                    return item
            return None

    def _submit(self, token, item: Dict):
        node = item["node"]
        engine = self._game.engines[node.next_player]
        has_clear_placements = any(n.clear_placements for n in node.nodes_from_root)
        if has_clear_placements or not hasattr(engine, "queries"):
            if not has_clear_placements:
                node.analyze(
                    engine,
                    priority=item["priority"],
                    visits=item["visits"],
                    analyze_fast=item["analyze_fast"],
                    time_limit=item["time_limit"],
                    report_every=item["report_every"],
                )
            self._finish_token(token)
            return

        def on_done(result, partial_result):
            node.set_analysis(result, partial_result=partial_result)
            if not partial_result:
                self._finish_token(token)

        def on_error(_err):
            self._finish_token(token)

        try:
            engine.request_analysis(
                node,
                callback=on_done,
                error_callback=on_error,
                priority=item["priority"],
                visits=item["visits"],
                analyze_fast=item["analyze_fast"],
                time_limit=item["time_limit"],
                report_every=item["report_every"],
            )
        except Exception:
            self._finish_token(token)
            raise

    def _finish_token(self, token):
        drain_cb = None
        with self._lock:
            if self._inflight.pop(token, None) is None:
                return
            self._inflight_items.pop(token, None)
            if not self._inflight and self._on_drained:
                drain_cb = self._on_drained
                self._on_drained = None
        if drain_cb:
            drain_cb()
        self.pump()
