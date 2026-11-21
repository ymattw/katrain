"""Lazy hover principal-variation (PV) refinement controller for BadukPanWidget."""

import time

from kivy.clock import Clock
from pysgf import Move

from katrain.core.constants import OUTPUT_DEBUG, OUTPUT_EXTRA_DEBUG, PONDERING_REPORT_DT, PRIORITY_ALTERNATIVES

HOVER_PV_MOVES = 10
HOVER_PV_DEBOUNCE = 0.35
HOVER_RESUME_DELAY = 2.0
HOVER_TIMEOUT_DELAY = 15.0


class HoverPVRefiner:
    """Coordinates pausing bulk analysis and streaming deeper PVs on board hover."""

    def __init__(self, board):
        self._board = board
        self._paused = False
        self._in_flight = 0
        self._game = None
        self.refine_trigger = Clock.create_trigger(self._refine_hover_pv, HOVER_PV_DEBOUNCE)
        self.resume_trigger = Clock.create_trigger(self._resume_bulk_analysis, HOVER_RESUME_DELAY)
        self.timeout_trigger = Clock.create_trigger(self._on_hover_timeout, HOVER_TIMEOUT_DELAY)

    def pick_best_pv(self, near_move):
        pv, node = max(near_move, key=lambda item: len(item[0]))
        katrain = self._board.katrain
        if len(near_move) > 1 and katrain:
            katrain.log(
                f"[hover-pv] {len(near_move)} entries at {pv[0] if pv else '?'}, picked len={len(pv)}",
                OUTPUT_EXTRA_DEBUG,
            )
        return pv, node

    def on_pv_cleared(self):
        self.refine_trigger.cancel()
        if self._paused and self._in_flight == 0:
            self.resume_trigger.cancel()
            self.resume_trigger()

    def on_pv_selected(self, pv, node):
        board = self._board
        anim = board.animating_pv
        if anim and anim[1] is node and anim[0] and pv and anim[0][0] == pv[0]:
            if anim[0] != pv:
                board.animating_pv = (pv, node, anim[2], board.last_mouse_pos)
            return
        board.animating_pv = (pv, node, time.time(), board.last_mouse_pos)
        self._maybe_schedule_refine(pv, node)

    def _sync_game(self, game):
        if self._game is not game:
            self._in_flight = 0
            self._paused = False
            self._game = game

    def _maybe_schedule_refine(self, pv, node):
        self.refine_trigger.cancel()
        katrain = self._board.katrain
        if not pv or node is None or not (katrain and katrain.game):
            return
        game = katrain.game
        if node is not game.current_node:
            return
        self._sync_game(game)

        gtp = pv[0]
        skip_reason = None
        if len(pv) >= HOVER_PV_MOVES:
            skip_reason = f"already {len(pv)} moves (>= {HOVER_PV_MOVES})"
        elif Move.from_gtp(gtp, player=node.next_player).is_pass:
            skip_reason = "pass move"
        elif gtp in getattr(node, "_hover_pv_requested", set()):
            skip_reason = "already requested"
        if skip_reason:
            katrain.log(f"[hover-pv] skip {gtp}: {skip_reason}", OUTPUT_EXTRA_DEBUG)
            if self._paused and self._in_flight == 0:
                self.resume_trigger.cancel()
                self.resume_trigger()
            return

        self.resume_trigger.cancel()
        game.pause_analysis()
        self._paused = True
        katrain.log(f"[hover-pv] schedule refine for {gtp} (cur len={len(pv)})", OUTPUT_DEBUG)
        self.refine_trigger()

    def _refine_hover_pv(self, *_args):
        board = self._board
        katrain = board.katrain
        if not board.animating_pv or not (katrain and katrain.game):
            return
        game = katrain.game
        self._sync_game(game)

        pv, node, _start_time, _pos = board.animating_pv
        if not pv or node is not game.current_node or len(pv) >= HOVER_PV_MOVES:
            return
        gtp = pv[0]
        move = Move.from_gtp(gtp, player=node.next_player)
        if move.is_pass or any(n.clear_placements for n in node.nodes_from_root):
            return
        requested = getattr(node, "_hover_pv_requested", None)
        if requested is None:
            node._hover_pv_requested = set()
            requested = node._hover_pv_requested
        if gtp in requested:
            return

        self.resume_trigger.cancel()
        game.pause_analysis()
        self._paused = True

        if self._in_flight > 0:
            return

        if game.analysis_inflight_count > 0:
            katrain.log(
                f"[hover-pv] waiting for {game.analysis_inflight_count} in-flight bulk queries to finish before refining {gtp}",
                OUTPUT_DEBUG,
            )
            game.set_on_analysis_drained(lambda: Clock.schedule_once(self._refine_hover_pv, 0))
            return

        engine = game.engines[node.next_player]
        requested.add(gtp)
        cur_visits = (node.analysis.get("moves", {}).get(gtp) or {}).get("visits", 0)
        visits = max(
            katrain.config("engine/max_visits", 500),
            cur_visits + katrain.config("engine/fast_visits", 25),
        )
        old_len = len(pv)
        self._in_flight += 1
        self.timeout_trigger.cancel()
        self.timeout_trigger()
        katrain.log(f"[hover-pv] request refine {gtp} (cur len={old_len}, visits={visits})", OUTPUT_DEBUG)
        try:
            engine.request_analysis(
                node,
                callback=lambda result, partial_result, n=node, m=move, ol=old_len: self._on_hover_refined(
                    n, m, ol, result, partial_result
                ),
                error_callback=lambda err, n=node, m=move: self._on_hover_error(n, m, err),
                next_move=move,
                visits=visits,
                priority=PRIORITY_ALTERNATIVES,
                time_limit=False,
                report_every=PONDERING_REPORT_DT,
            )
        except Exception:
            self._finish_hover_query()
            raise

    def _on_hover_refined(self, node, move, old_len, result, partial_result):
        board = self._board
        katrain = board.katrain
        node.set_analysis(result, refine_move=move, partial_result=partial_result)
        gtp = move.gtp()
        pvtail = result["moveInfos"][0]["pv"] if result.get("moveInfos") else []
        new_pv = [gtp] + pvtail
        cur = node.analysis.get("moves", {}).get(gtp)
        if cur is not None and len(new_pv) > len(cur.get("pv", [])):
            cur["pv"] = new_pv

        anim = board.animating_pv
        if anim and anim[1] is node and anim[0] and anim[0][0] == gtp and len(new_pv) > len(anim[0]):
            board.animating_pv = (new_pv, node, anim[2], anim[3])
            board.redraw_hover_contents_trigger()

        if partial_result:
            if katrain:
                katrain.log(f"[hover-pv] partial {gtp}: len {old_len} -> {len(new_pv)}", OUTPUT_EXTRA_DEBUG)
            self.timeout_trigger.cancel()
            self.timeout_trigger()
        else:
            if katrain:
                katrain.log(f"[hover-pv] done {gtp}: len {old_len} -> {len(new_pv)}", OUTPUT_DEBUG)
            self._finish_hover_query()

    def _on_hover_error(self, node, move, error):
        if self._board.katrain:
            self._board.katrain.log(f"[hover-pv] error refining {move.gtp()}: {error}", OUTPUT_DEBUG)
        self._finish_hover_query()

    def _finish_hover_query(self):
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self.timeout_trigger.cancel()
            board = self._board
            anim = board.animating_pv
            if anim and board.katrain and board.katrain.game:
                pv, node, _, _ = anim
                if (
                    pv
                    and node is board.katrain.game.current_node
                    and len(pv) < HOVER_PV_MOVES
                    and not Move.from_gtp(pv[0], player=node.next_player).is_pass
                    and pv[0] not in getattr(node, "_hover_pv_requested", set())
                ):
                    Clock.schedule_once(self._refine_hover_pv, 0)
                    return
            if self._paused:
                self.resume_trigger.cancel()
                self.resume_trigger()

    def _on_hover_timeout(self, *_args):
        if self._board.katrain:
            self._board.katrain.log("[hover-pv] hover refine timed out, resuming bulk analysis", OUTPUT_DEBUG)
        self._in_flight = 0
        self._resume_bulk_analysis()

    def _resume_bulk_analysis(self, *_args):
        if self._in_flight > 0:
            return
        self._paused = False
        if self._board.katrain and self._board.katrain.game:
            self._board.katrain.game.resume_analysis()
