# Undo for node deletion and branch pruning

Design document for `Ctrl+Z` / `Cmd+Z` undo of move-tree edits.

The deletion shortcut itself is documented separately in
[`docs/backspace-del.md`](backspace-del.md). This document assumes the move
tree can delete/prune nodes and explains how those edits are recorded and
restored.

The goal is to be self-contained: it should be possible to rebuild the undo
feature from scratch by following it, without reading the diff.

## 1. Summary

Undo is a *tree-edit* undo. It is unrelated to the existing "undo move"
navigation (`z` / `left arrow`), which only moves the current node pointer.

- `Ctrl+Z` / `Cmd+Z` restores the most recent deleted node (and its subtree)
  or the most recent branch removed by "Prune Branch".
- When there is nothing to restore, it is a no-op.
- If a new branch is played at the same position afterwards, the stored edit
  is dropped so the undo can no longer overwrite the new branch.

Modifier handling already exists in `KaTrainGui._on_keyboard_down`:

```python
ctrl_pressed = "ctrl" in modifiers or ("meta" in modifiers and kivy_platform == "macosx")
shift_pressed = "shift" in modifiers
```

So on macOS `Cmd` is reported as `ctrl_pressed`, which is what makes `Cmd+Z`
work without extra code.

## 2. Requirements

- `Ctrl+Z` / `Cmd+Z` restores the most recent deletion or prune.
- When there is nothing to restore, `Ctrl+Z` / `Cmd+Z` is a no-op.
- After a deletion, if a **new branch is played at the same position**, the
  stored deletion must be dropped so the undo can no longer overwrite the new
  branch.
- The undo history must not leak across games (load/new game).

## 3. Non-goals / limitations

- No redo of a deletion (`Ctrl+Shift+Z` is not wired to this stack).
- No confirmation prompt for deletion.
- Node creation paths that do not go through `BaseGame.play` are **not**
  hooked for invalidation (see §8).
- `Ctrl+Z` used to mean "jump to the start of the game" (via
  `self("undo", 9999)`). That behaviour is replaced for `Ctrl+Z`; it is still
  reachable via `Ctrl+Shift+Z`, `Home`, or `Ctrl+Left`.

## 4. Data model

The undo history lives on the `Game` object, so it is recreated together with
the game (this is what prevents cross-game restores).

```python
# BaseGame.__init__
self.tree_edit_undo_stack = []  # stack of tree edits for ctrl+z restore
```

Each element is a tagged tuple.

### 4.1 Delete record

```python
("delete", parent, child, index, shortcut_node)
```

- `parent`: the node whose `children` list was modified.
- `child`: the node that was removed from `parent.children` (may be a `via`
  node when a shortcut was deleted).
- `index`: position of `child` inside `parent.children` at deletion time.
- `shortcut_node`: if the deleted node was a collapsed-branch destination
  (`selected_node.shortcut_from is not None`), this holds `selected_node` so
  the shortcut can be re-added on restore; otherwise `None`.

### 4.2 Prune record

```python
("prune", selected_node, children_backup)
```

- `selected_node`: the node the user right-clicked to prune.
- `children_backup`: a list of `(parent, list(parent.children))` snapshots for
  every ancestor whose `children` was overwritten by the prune.

## 5. Core operations (`katrain/core/game.py`)

### 5.1 Recording a deletion

Called by the move tree widget **before** removing the node.

```python
def record_deletion(self, parent, child, index, shortcut_node=None):
    self.tree_edit_undo_stack.append(("delete", parent, child, index, shortcut_node))
```

### 5.2 Recording a prune

Called by the move tree widget with snapshots taken before mutating.

```python
def record_prune(self, selected_node, children_backup):
    self.tree_edit_undo_stack.append(("prune", selected_node, children_backup))
```

### 5.3 Undo (restore) the most recent edit

Returns the node that should become current, or `None` if the stack is empty.

```python
def undo_deletion(self):
    if not self.tree_edit_undo_stack:
        return None
    record = self.tree_edit_undo_stack.pop()
    with self._lock:
        if record[0] == "prune":
            _, selected_node, children_backup = record
            for parent, children in children_backup:
                parent.children = list(children)
            return selected_node
        _, parent, child, index, shortcut_node = record
        parent.children.insert(min(index, len(parent.children)), child)
        if shortcut_node is not None:
            parent.add_shortcut(shortcut_node)
        return shortcut_node if shortcut_node is not None else child
```

Notes:
- `min(index, len(...))` guards against an out-of-range index.
- Restoring a shortcut requires both re-inserting the `via` child and
  re-adding the shortcut (`GameNode.add_shortcut` recomputes `via` from the
  parent chain, which is intact because deletion never changes `parent`
  pointers).
- The method name `undo_deletion` is historical; it handles both record types.

### 5.4 Invalidation on new branch

When a new child is created under a parent that appears in a stored edit, the
edit can no longer be applied without clobbering the new branch, so it is
dropped.

```python
def _invalidate_undo_for_parent(self, parent):
    """Drop undoable tree edits that touched `parent`, since a new branch now occupies that position."""

    def touches(record):
        if record[0] == "delete":
            return record[1] is parent
        return any(p is parent for p, _children in record[2])

    self.tree_edit_undo_stack = [record for record in self.tree_edit_undo_stack if not touches(record)]
```

### 5.5 Hook into move creation

`BaseGame.play()` is the single place where human and AI moves create a node.
Capture the parent and whether the returned node is genuinely new
(`Node.play` returns an existing child when the same move already exists), and
invalidate only for genuinely new branches.

```python
parent = self.current_node
children_before = list(parent.children)
played_node = parent.play(move)
self.current_node = played_node
if not any(child is played_node for child in children_before):  # a new branch was created
    self._invalidate_undo_for_parent(parent)
```

## 6. Move tree widget (`katrain/gui/widgets/movetree.py`)

### 6.1 Delete

`MoveTreeCanvas.delete_selected_node` records the edit, then performs the
existing removal. The active node is `menu_selected_node` (right-click
selection) falling back to the current node.

```python
def delete_selected_node(self):
    selected_node = self.menu_selected_node or self.scroll_view_widget.current_node
    if selected_node and selected_node.parent:
        game = App.get_running_app().gui.game
        if selected_node.shortcut_from:
            parent = selected_node.shortcut_from
            via = [v for m, v in parent.shortcuts_to if m == selected_node]
            selected_node.remove_shortcut()
            if via:  # should always be
                child = via[0]
                game.record_deletion(parent, child, parent.children.index(child), shortcut_node=selected_node)
                parent.children.remove(child)
        else:
            parent = selected_node.parent
            game.record_deletion(parent, selected_node, parent.children.index(selected_node))
            parent.children.remove(selected_node)
        self.set_game_node(parent)
    self.is_open = False
```

### 6.2 Undo

```python
def undo_deletion(self):
    node = App.get_running_app().gui.game.undo_deletion()
    if node is not None:
        self.set_game_node(node)
        return True
    return False
```

### 6.3 Prune

Snapshot each ancestor's children before overwriting, and only record the
prune if it actually removed something (some ancestor had more than one child).
This avoids pushing no-op entries that would swallow a later `Ctrl+Z`.

```python
def prune_branch(self):
    selected_node = self.menu_selected_node
    if selected_node and selected_node.parent:
        game = App.get_running_app().gui.game
        children_backup = []
        node = selected_node
        while node.parent is not None:
            children_backup.append((node.parent, list(node.parent.children)))
            node.parent.children = [node]
            node = node.parent
        if any(len(children) > 1 for _parent, children in children_backup):
            game.record_prune(selected_node, children_backup)
        self.set_game_node(selected_node)
    self.is_open = False
```

### 6.4 `MoveTree` wrappers

The outer widget exposes thin wrappers that trigger a redraw. The keyboard
handler talks to these.

```python
def undo_deletion(self):
    if self.move_tree_canvas.undo_deletion():
        self.redraw_tree_trigger()
        return True
    return False
```

## 7. Keyboard wiring (`katrain/__main__.py`)

`KEY_NAV_PREV` is `["left", "z"]`. `Ctrl+Z` / `Cmd+Z` is carved out before
the normal undo-move handling:

```python
elif keycode[1] in Theme.KEY_NAV_PREV:
    if ctrl_pressed and not shift_pressed:
        self.controls.move_tree.undo_deletion()  # restore deleted/pruned branch; no-op if nothing to restore
    else:
        self("undo", 1 + shift_pressed * 9 + ctrl_pressed * 9999)
```

Behaviour of the `KEY_NAV_PREV` family after the change:

| key | action |
| --- | --- |
| `z` | undo 1 move |
| `Shift+Z` | undo 10 moves |
| `Ctrl+Z` / `Cmd+Z` | restore last deleted node / pruned branch, else no-op |
| `Ctrl+Shift+Z` | unchanged (falls through to the "jump to start" expression) |

## 8. Node-creation paths that are NOT invalidated

`_invalidate_undo_for_parent` is only called from `BaseGame.play`, which
covers human moves (`__main__.py` → `game.play(...)`) and engine moves
(`ai.py` → `game.play(...)`). The following create nodes directly and do not
invalidate (acceptable edge cases):

- `BaseGame.sync_branch()` (used by contribute self-play / some loading)
- self-play-to-end (`GameNode(parent=node, ...)` in `game.py`)
- Tsumego Frame insertion (`core/tsumego_frame.py`)
- middle-click principal-variation insertion (`gui/badukpan.py`)

If broader coverage is wanted, route these through the same hook or move the
invalidation to a lower-level node-insertion helper.

## 9. Threading

- The keyboard handler and the move-tree widget run on the Kivy main thread
  and mutate the tree directly, exactly like the pre-existing delete code.
- `undo_deletion` takes `BaseGame._lock` while mutating `parent.children`.
- `set_game_node` calls `game.set_current_node(node)` directly and then
  `katrain.update_state()`, where `update_state` is dispatched through the
  message queue.

## 10. Behaviour matrix

| operation | result | `Ctrl+Z` / `Cmd+Z` after it |
| --- | --- | --- |
| Delete node `X` | `X` removed from `parent.children`; current = parent | restores `X` at its original index |
| Delete shortcut node | shortcut removed, `via` child removed | re-inserts `via` and re-adds shortcut |
| Prune branch | other children removed along the path | restores every affected `children` list |
| Prune that changes nothing | only navigation, no record | undoes the previous edit (or no-op) |
| Play a new move under a parent in the stack | new child added | those edits are dropped; undo no longer restores them |
| Play a move that already exists | reuses child, no new branch | edits are kept |
| Navigate only | no tree change | edits are kept |
| New game / load game | new `Game` object | empty stack, no-op |

## 11. Rebuild checklist

1. `game.py`: add `tree_edit_undo_stack`, `record_deletion`, `record_prune`,
   `undo_deletion`, `_invalidate_undo_for_parent`, and the `play()` hook.
2. `movetree.py`: record on delete/prune, add `undo_deletion` to
   `MoveTreeCanvas` and `MoveTree`.
3. `__main__.py`: add the `Ctrl+Z` branch inside `KEY_NAV_PREV`.
4. Verify with the tests in §12.

## 12. Testing

Pure tree-logic tests can be written without a live engine by exercising the
`BaseGame` methods against a small `GameNode` tree (the methods only need
`self._lock` and `self.tree_edit_undo_stack`). Cover:

- delete a node, then undo: node returns at its original index.
- delete a shortcut destination, then undo: shortcut and `via` child return.
- delete several nodes, then undo repeatedly: reverse order.
- prune, then undo: all siblings along the path return.
- prune a no-op path: no record is pushed.
- play a new move under a deleted node's parent, then undo: record dropped,
  `undo_deletion()` returns `None`.
- play under an unrelated parent: record survives.
- empty stack: `undo_deletion()` returns `None`.

Also run the existing suite (move-tree/parser/board tests) and `ruff`.
