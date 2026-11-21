# Deleting a node with Backspace / Del

Design document for the move-tree node deletion shortcut.

The undo half of this feature is documented separately in
[`docs/undo.md`](undo.md). This document covers only the key handling that
deletes the current move-tree node.

## 1. Summary

Deleting the current node was previously only possible through the right-click
context menu ("Delete Node"), and its keyboard shortcut was `Ctrl+Del`.
This feature adds plain `Del` / `Backspace` (with or without `Ctrl`) and fixes
deletion on macOS.

## 2. Background: key names differ by platform

KaTrain receives key names from Kivy (`keycode[1]`). The relevant names are:

| physical action | SDL/keycode value | Kivy name |
| --- | --- | --- |
| delete backwards (`Backspace`) | 8 | `"backspace"` |
| delete forwards (`Delete`) | 127 | `"delete"` |

On macOS the key labelled **delete** (top-right of the main block) actually
sends `"backspace"`; forward-delete is `Fn`+delete and sends `"delete"`.
On a PC keyboard the `Backspace` key sends `"backspace"` and the `Del` key
sends `"delete"`.

This is why the old binding (`KEY_MOVE_TREE_DELETE_SELECTED_NODE = "delete"`)
did not work with the normal delete key on a Mac: that key reports as
`"backspace"`. The shortcut must listen to **both** names.

## 3. Requirements

- `Del`, `Backspace`, `Ctrl+Del`, `Ctrl+Backspace` delete the current
  move-tree node.
- `Shift`+delete/backspace must do nothing (avoid accidental deletion).
- Deleting must never interfere with typing in the note field.
- The right-click context menu keeps working unchanged.

## 4. Key constant (`katrain/gui/theme.py`)

```python
KEY_MOVE_TREE_DELETE_SELECTED_NODE = ["delete", "backspace"]
```

It is a list because Kivy reports two different names for the same intent
depending on platform and key position.

## 5. Keyboard handler (`katrain/__main__.py`)

Inside `KaTrainGui._on_keyboard_down`, after the note-field guard and the
popup guard:

```python
elif keycode[1] in Theme.KEY_MOVE_TREE_DELETE_SELECTED_NODE and not shift_pressed:
    self.controls.move_tree.delete_selected_node()
```

Notes:
- The check is `in` (the constant is a list).
- No modifier is required, so plain `Del`/`Backspace` work; `Ctrl` is also
  accepted for backwards compatibility with the old `Ctrl+Del` shortcut.
- `not shift_pressed` makes `Shift`+delete a no-op.
- On macOS `Ctrl` is not required at all; the normal delete key works.

## 6. Note field safety

`_on_keyboard_down` returns early while the note field has focus:

```python
if self.controls.note.focus:
    return  # when making notes, don't allow keyboard shortcuts
```

so `Backspace` still edits text there. Popups also return early, so text
inputs inside popups are unaffected. The note field is the only text input on
the main screen.

## 7. Context menu label (`katrain/gui/widgets/movetree.py`)

The right-click "Delete Node" item shows a short hint. It must stay short so it
does not overflow the row:

```python
MoveTreeDropdownItem:
    text: i18n._("Delete Node")
    icon: 'delete.png'
    shortcut: 'Del'
    on_action: root.katrain.controls.move_tree.delete_selected_node()
```

## 8. Interaction with undo

Deletion now records the edit so it can be restored with `Ctrl+Z` / `Cmd+Z`.
The recording lives in `delete_selected_node` and is documented in
[`docs/undo.md`](undo.md).

## 9. Behaviour matrix

| key | result |
| --- | --- |
| `Del` | delete current node |
| `Backspace` | delete current node |
| `Ctrl+Del` / `Ctrl+Backspace` | delete current node |
| `Shift+Del` / `Shift+Backspace` | no-op |
| `Del` / `Backspace` while note field has focus | edit note text |
| `Del` / `Backspace` while a popup is open | handled by the popup |

## 10. Rebuild checklist

1. `theme.py`: set `KEY_MOVE_TREE_DELETE_SELECTED_NODE = ["delete", "backspace"]`.
2. `__main__.py`: change the handler to `in ... and not shift_pressed`.
3. `movetree.py`: set the context-menu `shortcut` label to `'Del'`.
