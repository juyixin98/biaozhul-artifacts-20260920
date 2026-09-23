"""Session manager: owns bags, engines, sinks and checkpoints.

A *session* is one replay of one bag with a chosen topic filter. The manager is
the only place that mutates engines, so the FastAPI layer stays thin and the
concurrency rules live in exactly one module.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from .bagio import BagError, BagIndex, load_bag
from .checkpoints import Checkpoint, CheckpointError, CheckpointStore
from .config import Settings
from .engine import PlayState, ReplayEngine
from .sinks import MultiSink, RingSink, RosSink


class SessionError(Exception):
    pass


class Session:
    def __init__(
        self,
        session_id: str,
        index: BagIndex,
        bag_path: Path,
        engine: ReplayEngine,
        ring: RingSink,
        ros: RosSink | None,
        topics: frozenset[str] | None,
        store: CheckpointStore,
    ) -> None:
        self.id = session_id
        self.index = index
        self.bag_path = bag_path
        self.engine = engine
        self.ring = ring
        self.ros = ros
        self.topics = topics
        self.store = store
        self.lock = threading.RLock()
        self.checkpoint_ids: set[str] = set()

    def shutdown(self) -> None:
        self.engine.shutdown()
        if self.ros is not None:
            self.ros.close()


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.store = CheckpointStore(settings.checkpoint_dir)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._index_cache: dict[str, BagIndex] = {}

    # ------------------------------------------------------------- bags
    def resolve_bag(self, uri: str) -> Path:
        try:
            return self.settings.resolve_bag(uri)
        except PermissionError as exc:
            raise SessionError(str(exc)) from exc

    def get_index(self, bag_path: Path) -> BagIndex:
        key = bag_path.as_posix()
        with self._lock:
            idx = self._index_cache.get(key)
        if idx is None:
            idx = load_bag(bag_path)
            with self._lock:
                self._index_cache[key] = idx
        return idx

    def describe_bag(self, uri: str) -> dict[str, Any]:
        bag_path = self.resolve_bag(uri)
        return self.get_index(bag_path).summary()

    # ------------------------------------------------------------- sessions
    def create_session(
        self,
        uri: str,
        topics: list[str] | None = None,
        rate: float = 1.0,
        auto_play: bool = True,
    ) -> Session:
        bag_path = self.resolve_bag(uri)
        try:
            index = self.get_index(bag_path)
        except BagError as exc:
            raise SessionError(str(exc)) from exc

        topic_set = self._validate_topics(index, topics)
        ring = RingSink(self.settings.ring_capacity)
        sinks: list[Any] = [ring]
        ros_sink: RosSink | None = None
        if self.settings.ros_enabled:
            try:
                from . import ros_runtime

                node = ros_runtime.init_node()
                active_types = {
                    name: t.type
                    for name, t in index.topics.items()
                    if topic_set is None or name in topic_set
                }
                ros_sink = RosSink(
                    node, active_types, prefix=self.settings.topic_prefix
                )
                sinks.append(ros_sink)
            except Exception:
                # No DDS / missing message packages: degrade to ring-only so
                # the API remains usable. Real publishing absence is surfaced
                # in status, not fatal.
                ros_sink = None

        engine = ReplayEngine(
            index=index,
            sink=MultiSink(sinks),
            topics=topic_set,
            rate=rate,
            auto_play=auto_play,
        )
        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id=session_id,
            index=index,
            bag_path=bag_path,
            engine=engine,
            ring=ring,
            ros=ros_sink,
            topics=topic_set,
            store=self.store,
        )
        with self._lock:
            self._sessions[session_id] = session
        engine.start()
        return session

    @staticmethod
    def _validate_topics(
        index: BagIndex, topics: list[str] | None
    ) -> frozenset[str] | None:
        if topics is None:
            return None
        selected = frozenset(topics)
        unknown = sorted(selected - set(index.topics))
        if unknown:
            raise SessionError(
                f"topics not present in bag: {', '.join(unknown)}"
            )
        if not selected:
            raise SessionError("topic filter is empty")
        return selected

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"session not found: {session_id}")
        return session

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.shutdown()

    def shutdown_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.shutdown()
        if self.settings.ros_enabled:
            try:
                from . import ros_runtime

                ros_runtime.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------- checkpoints
    def save_checkpoint(self, session_id: str, checkpoint_id: str | None) -> Checkpoint:
        session = self.get(session_id)
        with session.lock:
            st = session.engine.status()
            cp_id = checkpoint_id or f"cp-{session.id}-{uuid.uuid4().hex[:8]}"
            cp = self.store.save(
                checkpoint_id=cp_id,
                index=session.index,
                topics=session.topics,
                next_seq=st["next_seq"],
                next_timestamp_ns=st["next_timestamp_ns"],
                rate=st["rate"],
                state=st["state"],
                generation=st["generation"],
            )
            session.checkpoint_ids.add(cp_id)
        return cp

    def restore_checkpoint(
        self, checkpoint_id: str, auto_play: bool | None = None
    ) -> Session:
        cp = self.store.load(checkpoint_id)
        bag_uri = cp.bag.get("uri")
        if not bag_uri:
            raise CheckpointError("checkpoint missing bag uri")
        bag_path = self.resolve_bag(bag_uri)

        # Source integrity gate: refuse to restore against a changed bag.
        ok, reason = self.store.verify_source(cp, bag_path)
        if not ok:
            raise CheckpointError(
                f"refusing to restore: source bag changed ({reason})"
            )

        index = self.get_index(bag_path)

        # Structural consistency: message count must match the digest-era bag.
        saved_count = cp.bag.get("message_count")
        if saved_count is not None and saved_count != len(index):
            raise CheckpointError(
                "refusing to restore: bag message count changed despite "
                "matching file digest"
            )

        topics = frozenset(cp.topics) if cp.topics else None
        self._validate_topics(index, list(topics) if topics else None)

        # Reconstruct a fresh session (new engine) at the saved position.
        session = self.create_session(
            uri=bag_uri,
            topics=sorted(topics) if topics else None,
            rate=cp.rate,
            auto_play=False,
        )
        target_seq = cp.position.get("next_seq")
        want_play = cp.state == PlayState.PLAYING.value if auto_play is None else auto_play
        with session.lock:
            if target_seq is None:
                # Saved at the tail: nothing left to publish.
                session.engine.seek(
                    target_seq=len(index.filtered_entries(topics)),
                    play=False,
                )
            else:
                filtered = index.filtered_entries(topics)
                # next_seq is a canonical global seq; find its filtered cursor.
                cursor = self._cursor_for_seq(filtered, int(target_seq))
                session.engine.seek(target_seq=cursor, play=want_play)
        session.checkpoint_ids.add(checkpoint_id)
        return session

    @staticmethod
    def _cursor_for_seq(entries: list, seq: int) -> int:
        lo, hi = 0, len(entries)
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid].seq < seq:
                lo = mid + 1
            else:
                hi = mid
        return lo
