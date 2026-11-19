"""Tests for KaTrain-side bulk analysis scheduling and hover PV pause/resume."""

import queue
import threading
import time

import pytest

from katrain.core.base_katrain import KaTrainBase
from katrain.core.engine import BaseEngine, KataGoEngine
from katrain.core.game import Game, Move
from katrain.core.game_node import GameNode


class MockKaTrain(KaTrainBase):
    def __init__(self, **kwargs):
        super().__init__(force_package_config=True, debug_level=0, **kwargs)
        self.update_state_calls = 0

    def update_state(self, redraw_board=False):
        self.update_state_calls += 1


class QueuedStubEngine(KataGoEngine):
    """Stub engine that captures analysis requests so tests can drive completion."""

    def __init__(self, katrain, analysis_threads=2):
        BaseEngine.__init__(self, katrain, {})
        self.allow_recovery = False
        self.queries = {}
        self.ponder_query = None
        self.query_counter = 0
        self.katago_process = None
        self.base_priority = 0
        self.query_generation = 0
        self.write_queue = queue.Queue()
        self.thread_lock = threading.RLock()
        self.analysis_thread = self.stderr_thread = self.write_stdin_thread = None
        self.analysis_threads = analysis_threads
        self.pending = []
        self.terminated_nodes = []
        self.stop_pondering_calls = 0

    def stop_pondering(self):
        self.stop_pondering_calls += 1

    def terminate_queries(self, only_for_node=None):
        self.terminated_nodes.append(only_for_node)
        with self.thread_lock:
            for qid in [
                k for k, (_cb, _ecb, _ts, _nm, n) in self.queries.items() if only_for_node is None or n is only_for_node
            ]:
                self.queries.pop(qid, None)

    def request_analysis(self, analysis_node, callback, error_callback=None, visits=None, **kwargs):
        with self.thread_lock:
            self.query_counter += 1
            qid = f"Q:{self.query_counter}"
            self.queries[qid] = (callback, error_callback, time.time(), kwargs.get("next_move"), analysis_node)
            self.pending.append((qid, analysis_node, callback, error_callback, visits, kwargs))
        analysis_node.analysis_visits_requested = max(analysis_node.analysis_visits_requested, visits or 500)

    def complete(self, index=0, partial=False, error=False):
        with self.thread_lock:
            qid, node, callback, error_callback, visits, _kwargs = self.pending[index]
            if not partial:
                self.pending.pop(index)
                self.queries.pop(qid, None)
        if error:
            if error_callback:
                error_callback({"id": qid, "noResults": True})
            return node
        payload = {
            "moveInfos": [
                {
                    "move": "D4",
                    "visits": visits or 500,
                    "order": 0,
                    "scoreLead": 0.5,
                    "winrate": 0.52,
                    "pv": ["D4", "Q16"],
                }
            ],
            "rootInfo": {"visits": visits or 500, "scoreLead": 0.5, "winrate": 0.52},
        }
        callback(payload, partial)
        return node


def wait_until(pred, timeout=5.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(interval)
    return pred()


def build_linear_tree(num_moves=5):
    root = GameNode(properties={"SZ": 19})
    node = root
    coords = ["D4", "Q16", "D16", "Q4", "C6", "R14", "C14", "R6"]
    for i in range(num_moves):
        node = node.play(Move.from_gtp(coords[i], player="BW"[i % 2]))
    return root


@pytest.fixture
def katrain():
    return MockKaTrain()


def test_scheduler_limits_inflight_and_pumps_sequentially(katrain):
    root = build_linear_tree(4)  # 5 nodes total (root + 4 moves)
    engine = QueuedStubEngine(katrain, analysis_threads=2)
    game = Game(katrain, engine, move_tree=root)

    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 3)
    assert game.analysis_inflight_count == 2
    assert game.queued_analyses == 3

    # Completing one query pumps the next queued node without exceeding limit=2.
    first_node = engine.complete(0)
    assert first_node.analysis_complete
    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 2)
    assert game.analysis_inflight_count == 2

    # Completing with error/noResults also frees the slot and pumps next node.
    engine.complete(0, error=True)
    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 1)

    while engine.pending:
        engine.complete(0)
    assert wait_until(lambda: game.analysis_inflight_count == 0 and game.queued_analyses == 0)


def test_pause_drains_and_resume_preserves_completed_nodes(katrain):
    root = build_linear_tree(4)
    engine = QueuedStubEngine(katrain, analysis_threads=2)
    game = Game(katrain, engine, move_tree=root)

    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 3)

    # Pause stops new submissions while letting in-flight queries finish.
    assert game.pause_analysis(terminate=False) is True
    assert game.analysis_paused is True
    assert engine.stop_pondering_calls >= 1

    drained = []
    game.set_on_analysis_drained(lambda: drained.append(True))
    assert drained == []

    done0 = engine.complete(0)
    assert game.analysis_inflight_count == 1
    assert game.queued_analyses == 3  # no new nodes pumped while paused
    assert drained == []

    done1 = engine.complete(0)
    assert wait_until(lambda: drained == [True])
    assert game.analysis_inflight_count == 0
    assert game.queued_analyses == 3

    # Resuming pumps remaining nodes without clearing already-completed nodes.
    game.resume_analysis()
    assert game.analysis_paused is False
    assert done0.analysis_complete
    assert done1.analysis_complete
    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 1)

    submitted_nodes = [item[1] for item in engine.pending]
    assert done0 not in submitted_nodes
    assert done1 not in submitted_nodes


def test_set_current_node_prioritizes_current_and_parent(katrain):
    root = build_linear_tree(5)  # nodes: root, m1, m2, m3, m4, m5
    nodes = root.nodes_in_tree
    engine = QueuedStubEngine(katrain, analysis_threads=1)
    game = Game(katrain, engine, move_tree=root)

    # First node submitted is root; m1..m5 are queued.
    assert wait_until(lambda: len(engine.pending) == 1 and game.queued_analyses == 5)

    # Navigate to m5; m5 (current) and m4 (parent) should jump ahead of m1..m3.
    game.set_current_node(nodes[5])
    engine.complete(0)
    assert wait_until(lambda: len(engine.pending) == 1 and engine.pending[0][1] is nodes[5])

    engine.complete(0)
    assert wait_until(lambda: len(engine.pending) == 1 and engine.pending[0][1] is nodes[4])


def test_pause_with_terminate_requeues_unfinished_at_front(katrain):
    root = build_linear_tree(4)
    engine = QueuedStubEngine(katrain, analysis_threads=2)
    game = Game(katrain, engine, move_tree=root)

    assert wait_until(lambda: len(engine.pending) == 2 and game.queued_analyses == 3)
    inflight_nodes = [item[1] for item in engine.pending]

    drained = []
    game.set_on_analysis_drained(lambda: drained.append(True))

    game.pause_analysis(terminate=True)
    assert game.analysis_inflight_count == 0
    assert drained == [True]
    assert set(engine.terminated_nodes) == set(inflight_nodes)
    assert game.queued_analyses == 5  # 2 re-queued + 3 original

    # Simulate KataGo sending late noResults for terminated queries.
    engine.complete(0, error=True)
    engine.complete(0, error=True)

    # Resuming pumps the interrupted nodes first.
    game.resume_analysis()
    assert wait_until(lambda: len(engine.pending) == 2)
    resumed_nodes = [item[1] for item in engine.pending]
    assert resumed_nodes == inflight_nodes


def test_clear_analysis_resets_hover_requested_flag():
    node = GameNode(properties={"SZ": 19})
    node._hover_pv_requested.add("D4")
    node.clear_analysis()
    assert node._hover_pv_requested == set()
