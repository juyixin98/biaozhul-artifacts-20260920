"""Treap/rope block index with path-local invalidation.

The document is a treap keyed by character offset: every leaf stores one
mask-safe block (its raw text and the block-local :class:`Reduction`); every
node caches its subtree length and composed subtree reduction in
*subtree-local* coordinates.

Edit surgery uses rope ``split`` / ``merge``:

1. map the edit range to whole blocks (plus successor blocks while the
   rebuilt window still ends inside an open quote/comment);
2. split the treap at the window boundaries -- only the discarded middle
   subtree is deleted;
3. re-lex and re-chunk only that window;
4. merge the pieces back and ``pull`` (recompute summary) only along the
   touched root paths.

Blocks outside the window are never re-read or re-lexed: this is the
"invalidate affected paths, do not rescan unrelated text" guarantee. Block
boundaries are mask-safe by construction, so no lexical state leaks into a
neighbour (the rightward open-mask extension is the one enforced exception).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from .chunker import chunk_text
from .lexer import Lexer
from ..storage import Database, decode_reduction, encode_reduction
from .tokens import EMPTY, Reduction, combine, reduce_tokens


@dataclass
class _Node:
    id: Optional[int]
    priority: int
    text: str
    left: Optional[int] = None
    right: Optional[int] = None
    sub_len: int = 0
    # own: block-LOCAL reduction of this node's text (immutable per text).
    # agg: cached subtree reduction in subtree-local coordinates, recomputed
    # by _pull. They MUST stay separate: every treap node is both a block
    # leaf and an internal node, and overwriting own with agg would make
    # later compositions count blocks multiple times.
    own: Reduction = field(default_factory=lambda: EMPTY)
    agg: Reduction = field(default_factory=lambda: EMPTY)


class BlockTreap:
    """One treap for one document; nodes are mirrored in ``bracket_blocks``."""

    def __init__(
        self,
        db: Database,
        document_id: int,
        lexer: Lexer,
        chunk_size: int,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._doc_id = document_id
        self._lexer = lexer
        self._chunk_size = chunk_size
        self._rng = rng or random.Random()
        self._nodes: dict[int, _Node] = {}
        self._root: Optional[int] = None
        self.last_edit_stats: dict = {
            "window": (0, 0),
            "blocks_removed": 0,
            "blocks_added": 0,
            "rescanned_chars": 0,
            "mask_blocks_absorbed": 0,
        }
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        rows = self._db.conn.execute(
            "SELECT id, priority, left_id, right_id, text, sub_len, red_json "
            "FROM bracket_blocks WHERE document_id = ?",
            (self._doc_id,),
        ).fetchall()
        if not rows:
            return
        root_rows = self._db.conn.execute(
            "SELECT id FROM bracket_blocks WHERE document_id = ? "
            "AND id NOT IN (SELECT left_id FROM bracket_blocks "
            "  WHERE left_id IS NOT NULL AND document_id = ?) "
            "AND id NOT IN (SELECT right_id FROM bracket_blocks "
            "  WHERE right_id IS NOT NULL AND document_id = ?)",
            (self._doc_id, self._doc_id, self._doc_id),
        ).fetchall()
        for r in rows:
            node = _Node(
                id=r["id"],
                priority=r["priority"],
                text=r["text"],
                left=r["left_id"],
                right=r["right_id"],
                sub_len=r["sub_len"],
            )
            node.own = decode_reduction(r["red_json"])
            self._nodes[node.id] = node
        roots = [r["id"] for r in root_rows]
        self._root = roots[0] if roots else None
        # Aggregate caches are derived, not persisted: rebuild once in
        # postorder after every node/edge is in place.
        if self._root is not None:
            self._recompute_caches(self._root)

    def _recompute_caches(self, nid: int) -> None:
        node = self._nodes[nid]
        if node.left is not None:
            self._recompute_caches(node.left)
        if node.right is not None:
            self._recompute_caches(node.right)
        self._pull(node)

    # ------------------------------------------------------------- mutations
    def _insert_row(self, node: _Node) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO bracket_blocks(document_id, priority, left_id, "
            "right_id, text, sub_len, red_json) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                self._doc_id,
                node.priority,
                node.left,
                node.right,
                node.text,
                node.sub_len,
                encode_reduction(node.own),
            ),
        )
        node.id = int(cur.lastrowid)
        self._nodes[node.id] = node
        return node.id

    def _write_row(self, node: _Node) -> None:
        assert node.id is not None
        self._db.conn.execute(
            "UPDATE bracket_blocks SET priority=?, left_id=?, right_id=?, "
            "text=?, sub_len=?, red_json=? WHERE id=? AND document_id=?",
            (
                node.priority,
                node.left,
                node.right,
                node.text,
                node.sub_len,
                encode_reduction(node.own),
                node.id,
                self._doc_id,
            ),
        )

    def _delete_subtree(self, root_id: Optional[int]) -> None:
        if root_id is None:
            return
        stack = [root_id]
        ids: list[int] = []
        while stack:
            nid = stack.pop()
            node = self._nodes[nid]
            ids.append(nid)
            if node.left is not None:
                stack.append(node.left)
            if node.right is not None:
                stack.append(node.right)
        placeholders = ",".join("?" for _ in ids)
        self._db.conn.execute(
            f"DELETE FROM bracket_blocks WHERE id IN ({placeholders})", ids
        )
        for nid in ids:
            del self._nodes[nid]

    def _new_leaf(self, text: str, allow_open_tail: bool = False) -> _Node:
        tokens, _, tail = self._lexer.scan_block(text, None)
        if tail is not None and not allow_open_tail:
            # Must not happen: callers only hand us mask-safe window pieces.
            raise AssertionError("non-final block ends inside an open mask")
        node = _Node(
            id=None,
            # 63 bits: SQLite stores signed 64-bit integers.
            priority=self._rng.getrandbits(63),
            text=text,
        )
        node.own = reduce_tokens(tokens)
        node.sub_len = len(text)
        node.agg = node.own
        self._insert_row(node)
        return node

    # ------------------------------------------------------------ treap ops
    def _pull(self, node: _Node) -> None:
        """Recompute subtree length and composed reduction (subtree-local)."""
        acc = EMPTY
        total = 0
        if node.left is not None:
            left = self._nodes[node.left]
            acc = combine(acc, left.agg, total)
            total += left.sub_len
        acc = combine(acc, node.own, total)
        total += len(node.text)
        if node.right is not None:
            right = self._nodes[node.right]
            acc = combine(acc, right.agg, total)
            total += right.sub_len
        node.sub_len = total
        node.agg = acc

    def _split(
        self, root_id: Optional[int], k: int
    ) -> tuple[Optional[int], Optional[int]]:
        """Split after the first ``k`` characters (0 <= k <= sub_len)."""
        if root_id is None:
            return None, None
        node = self._nodes[root_id]
        left_len = self._nodes[node.left].sub_len if node.left is not None else 0
        own_len = len(node.text)
        if k < left_len:
            a, b = self._split(node.left, k)
            node.left = b
            self._pull(node)
            self._write_row(node)
            return a, node.id
        if k > left_len + own_len:
            a, b = self._split(node.right, k - left_len - own_len)
            node.right = a
            self._pull(node)
            self._write_row(node)
            return node.id, b
        if k == left_len:
            left = node.left
            node.left = None
            self._pull(node)
            self._write_row(node)
            return left, node.id
        # k == left_len + own_len: cut between this node and its right subtree
        right = node.right
        node.right = None
        self._pull(node)
        self._write_row(node)
        return node.id, right

    def _merge(
        self, a_id: Optional[int], b_id: Optional[int]
    ) -> Optional[int]:
        if a_id is None:
            return b_id
        if b_id is None:
            return a_id
        a = self._nodes[a_id]
        b = self._nodes[b_id]
        if a.priority > b.priority:
            a.right = self._merge(a.right, b_id)
            self._pull(a)
            self._write_row(a)
            return a.id
        b.left = self._merge(a_id, b.left)
        self._pull(b)
        self._write_row(b)
        return b.id

    # ------------------------------------------------------------- building
    def build(self, text: str) -> None:
        """Build a fresh treap from the full document text."""
        if self._root is not None:
            self._delete_subtree(self._root)
            self._root = None
        chunks = chunk_text(text, self._lexer, self._chunk_size)
        for idx, chunk in enumerate(chunks):
            allow_tail = idx == len(chunks) - 1
            nid = self._new_leaf(chunk, allow_open_tail=allow_tail).id
            self._root = self._merge(self._root, nid)

    def _build_from_chunks(self, chunks: list[str]) -> Optional[int]:
        root: Optional[int] = None
        for idx, chunk in enumerate(chunks):
            allow_tail = idx == len(chunks) - 1
            nid = self._new_leaf(chunk, allow_open_tail=allow_tail).id
            root = self._merge(root, nid)
        return root

    # ------------------------------------------------------------- locating
    @property
    def length(self) -> int:
        return self._nodes[self._root].sub_len if self._root is not None else 0

    def _locate_block(self, offset: int) -> tuple[int, int]:
        """Return ``(block_start, node_id)`` containing ``offset``.

        ``offset == length`` maps to the last block (used for end-anchored
        edits). Raises IndexError on an empty document.
        """
        if self._root is None or not (0 <= offset <= self.length):
            raise IndexError("offset out of range")
        nid = self._root
        base = 0
        target = min(offset, self.length - 1)
        while True:
            node = self._nodes[nid]
            left_len = self._nodes[node.left].sub_len if node.left else 0
            own_start = base + left_len
            own_end = own_start + len(node.text)
            if target < own_start:
                nid = node.left  # type: ignore[assignment]
            elif target >= own_end:
                base = own_end
                nid = node.right  # type: ignore[assignment]
            else:
                return own_start, nid

    def _block_bounds(self, start: int, end: int) -> tuple[int, int]:
        """Expand [start, end) to whole-block boundaries."""
        if self.length == 0:
            return 0, 0
        win_start, _ = self._locate_block(start)
        if end >= self.length:
            return win_start, self.length
        last_start, last_id = self._locate_block(max(start, end - 1))
        win_end = last_start + len(self._nodes[last_id].text)
        return win_start, win_end

    # ---------------------------------------------------------------- editing
    def edit(self, start: int, end: int, replacement: str) -> int:
        """Replace [start, end) with ``replacement``; returns new length."""
        if not (0 <= start <= end <= self.length):
            raise IndexError("edit range out of bounds")
        if self._root is None and (start or end):
            raise IndexError("edit range out of bounds")

        if self._root is None:
            self.build(replacement)
            return self.length

        win_start, win_end = self._block_bounds(start, end)
        left, rest = self._split(self._root, win_start)
        middle, right = self._split(rest, win_end - win_start)

        window_text = self._inorder_text(middle)
        removed = self._subtree_size(middle)
        self._delete_subtree(middle)

        local_start = start - win_start
        local_end = end - win_start
        new_window = (
            window_text[:local_start] + replacement + window_text[local_end:]
        )

        # Open-mask propagation: if the rebuilt window still runs into an
        # unterminated quote/block comment, absorb one successor block and
        # retry, so masking of unchanged blocks cannot silently change.
        absorbed = 0
        while right is not None and self._lexer.ends_in_open_mask(new_window):
            succ_id = self._first_leaf(right)
            succ_len = len(self._nodes[succ_id].text)
            pulled, right = self._split(right, succ_len)
            pulled_text = self._inorder_text(pulled)
            removed += self._subtree_size(pulled)
            self._delete_subtree(pulled)
            new_window += pulled_text
            win_end += succ_len
            absorbed += 1

        chunks = chunk_text(new_window, self._lexer, self._chunk_size)
        before_count = len(self._nodes)
        new_mid = self._build_from_chunks(chunks)
        added = len(self._nodes) - before_count
        self._root = self._merge(self._merge(left, new_mid), right)
        self.last_edit_stats = {
            "window": (win_start, win_end),
            "blocks_removed": removed,
            "blocks_added": added,
            # Only window text (plus absorbed successor blocks) was re-lexed;
            # never the document outside it.
            "rescanned_chars": len(new_window),
            "mask_blocks_absorbed": absorbed,
        }
        return self.length

    def _subtree_size(self, root_id: Optional[int]) -> int:
        if root_id is None:
            return 0
        count = 0
        stack = [root_id]
        while stack:
            nid = stack.pop()
            node = self._nodes[nid]
            count += 1
            if node.left is not None:
                stack.append(node.left)
            if node.right is not None:
                stack.append(node.right)
        return count

    # ------------------------------------------------------------- readings
    def _first_leaf(self, root_id: int) -> int:
        nid = root_id
        while self._nodes[nid].left is not None:
            nid = self._nodes[nid].left  # type: ignore[assignment]
        return nid

    def _inorder_text(self, root_id: Optional[int]) -> str:
        if root_id is None:
            return ""
        parts: list[str] = []
        stack: list[int] = []
        nid: Optional[int] = root_id
        while nid is not None or stack:
            while nid is not None:
                stack.append(nid)
                nid = self._nodes[nid].left
            nid = stack.pop()
            parts.append(self._nodes[nid].text)
            nid = self._nodes[nid].right
        return "".join(parts)

    def full_text(self) -> str:
        return self._inorder_text(self._root)

    def root_reduction(self) -> Reduction:
        """Document-wide reduction in global coordinates (subtree-local at
        root == global)."""
        if self._root is None:
            return EMPTY
        return self._nodes[self._root].agg

    def block_count(self) -> int:
        return len(self._nodes)

    def leaf_segments(self) -> list[tuple[int, int, str]]:
        """In-order ``(row_id, global_start, text)`` of every leaf block.

        Test/diagnostic helper: row ids of untouched leaves stay stable across
        an edit, which is how tests prove unrelated text was not rescanned.
        """
        out: list[tuple[int, int, str]] = []
        if self._root is None:
            return out

        def walk(nid: Optional[int], base: int) -> int:
            if nid is None:
                return base
            node = self._nodes[nid]
            left_len = self._nodes[node.left].sub_len if node.left else 0
            pos = walk(node.left, base)
            own_start = base + left_len
            out.append((nid, own_start, node.text))
            return walk(node.right, own_start + len(node.text))

        walk(self._root, 0)
        return out
