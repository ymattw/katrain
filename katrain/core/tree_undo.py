"""Undo stack for destructive move-tree edits (node deletion and branch pruning)."""

from contextlib import nullcontext


class TreeEditUndoStack:
    """Records move-tree deletions and prunes so they can be restored with Ctrl/Cmd+Z."""

    def __init__(self):
        self._stack = []

    def __len__(self):
        return len(self._stack)

    def __bool__(self):
        return bool(self._stack)

    def record_deletion(self, parent, child, index, shortcut_node=None):
        self._stack.append(("delete", parent, child, index, shortcut_node))

    def record_prune(self, selected_node, children_backup):
        self._stack.append(("prune", selected_node, children_backup))

    def invalidate_for_parent(self, parent):
        """Drop edits touching `parent`, since a new branch now occupies that position."""

        def touches(record):
            if record[0] == "delete":
                return record[1] is parent
            return any(p is parent for p, _children in record[2])

        self._stack = [record for record in self._stack if not touches(record)]

    def undo(self, lock=None):
        """Restore the most recent tree edit and return the node to select, or None."""
        if not self._stack:
            return None
        record = self._stack.pop()
        ctx = lock if lock is not None else nullcontext()
        with ctx:
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
