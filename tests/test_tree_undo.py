"""Tests for move-tree deletion and branch-pruning undo."""

import threading

from pysgf import Move

from katrain.core.game_node import GameNode
from katrain.core.tree_undo import TreeEditUndoStack


def _build_branching_tree():
    root = GameNode(properties={"SZ": 19})
    m1 = root.play(Move.from_gtp("D4", player="B"))
    m2a = m1.play(Move.from_gtp("Q16", player="W"))
    m2b = m1.play(Move.from_gtp("D16", player="W"))
    m2c = m1.play(Move.from_gtp("Q4", player="W"))
    return root, m1, m2a, m2b, m2c


def test_undo_empty_returns_none():
    stack = TreeEditUndoStack()
    assert stack.undo() is None


def test_undo_restores_deleted_node_at_original_index():
    _root, m1, m2a, m2b, m2c = _build_branching_tree()
    stack = TreeEditUndoStack()

    idx = m1.children.index(m2b)
    stack.record_deletion(m1, m2b, idx)
    m1.children.remove(m2b)
    assert m1.children == [m2a, m2c]

    restored = stack.undo(threading.RLock())
    assert restored is m2b
    assert m1.children == [m2a, m2b, m2c]


def test_undo_restores_shortcut_node_and_via_child():
    root, m1, m2a, _m2b, _m2c = _build_branching_tree()
    m3 = m2a.play(Move.from_gtp("C6", player="B"))
    m1.add_shortcut(m3)
    assert m3.shortcut_from is m1

    stack = TreeEditUndoStack()
    via = [v for m, v in m1.shortcuts_to if m == m3]
    assert via == [m2a]
    m3.remove_shortcut()
    stack.record_deletion(m1, m2a, m1.children.index(m2a), shortcut_node=m3)
    m1.children.remove(m2a)

    restored = stack.undo()
    assert restored is m3
    assert m2a in m1.children
    assert m3.shortcut_from is m1


def test_undo_multiple_deletions_in_reverse_order():
    _root, m1, m2a, m2b, m2c = _build_branching_tree()
    stack = TreeEditUndoStack()

    stack.record_deletion(m1, m2b, m1.children.index(m2b))
    m1.children.remove(m2b)
    stack.record_deletion(m1, m2c, m1.children.index(m2c))
    m1.children.remove(m2c)

    assert stack.undo() is m2c
    assert stack.undo() is m2b
    assert m1.children == [m2a, m2b, m2c]
    assert stack.undo() is None


def test_undo_restores_pruned_branch():
    root, m1, m2a, m2b, m2c = _build_branching_tree()
    stack = TreeEditUndoStack()

    children_backup = [(m1, list(m1.children)), (root, list(root.children))]
    m1.children = [m2b]
    root.children = [m1]
    stack.record_prune(m2b, children_backup)

    restored = stack.undo()
    assert restored is m2b
    assert m1.children == [m2a, m2b, m2c]


def test_invalidate_drops_records_for_parent_but_keeps_unrelated():
    root, m1, m2a, m2b, _m2c = _build_branching_tree()
    stack = TreeEditUndoStack()

    stack.record_deletion(m1, m2b, m1.children.index(m2b))
    m1.children.remove(m2b)

    # Playing under an unrelated parent keeps the record intact.
    stack.invalidate_for_parent(root)
    assert len(stack) == 1

    # Playing a new branch under m1 drops the record.
    stack.invalidate_for_parent(m1)
    assert len(stack) == 0
    assert stack.undo() is None
