"""In-process registry of replay sessions."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import bagstore
from .bagstore import BagIndex
from .checkpoints import CheckpointStore, SourceChangedError
from .config import Settings
from .playback import ReplayEngine
from .sinks import LoopbackSink, RosSink, Sink


@dataclass
class Session:
    id: str
    index: BagIndex
    engine: ReplayEngine
    sink: Sink
    transport: str
    checkpoint_store: CheckpointStore
    bag_dir: Path


class SessionManager:
    def __init__(self, settings: Settings, key: bytes) -> None:
        self.settings = settings
        self._key = key
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    # ------------------------------------------------------------ create

    def create(
        self,
        bag_uri: str,
        *,
        topics: Optional[list[str]] = None,
        rate: float = 1.0,
        autoplay: bool = False,
        transport: Optional[str] = None,
    ) -> Session:
        bag_dir = bagstore.resolve_bag_uri(bag_uri, self.settings.bag_roots)
        index, payloads = bagstore.index_bag_with_payloads(bag_dir)
        if topics:
            unknown = sorted(set(topics) - set(index.topic_names()))
            if unknown:
                raise ValueError(f"topics not in bag: {unknown}")

        transport = transport or self.settings.transport
        session_id = uuid.uuid4().hex[:16]
        sink = self._build_sink(transport, session_id)
        cp_store = CheckpointStore(self.settings.state_dir / session_id, self._key)
        engine = ReplayEngine(
            index,
            sink,
            tick_seconds=self.settings.tick_seconds,
            topic_filter=topics,
            session_id=session_id,
            payloads=payloads,
        )
        engine.set_rate(rate)
        if autoplay:
            engine.play()

        session = Session(
            id=session_id,
            index=index,
            engine=engine,
            sink=sink,
            transport=transport,
            checkpoint_store=cp_store,
            bag_dir=bag_dir,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def _build_sink(self, transport: str, session_id: str) -> Sink:
        if transport == "loopback":
            return LoopbackSink()
        if transport == "ros":
            return RosSink(session_id, self.settings.control_topic)
        raise ValueError(f"unknown transport {transport!r} (use loopback|ros)")

    # ------------------------------------------------------------ lookup

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [
            {
                "session_id": s.id,
                "bag_uri": s.index.uri,
                "transport": s.transport,
                **s.engine.position(),
            }
            for s in sessions
        ]

    def delete(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(session_id)
        session.engine.shutdown()

    def shutdown_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.engine.shutdown()

    # ------------------------------------------------------- checkpoints

    def save_checkpoint(
        self, session: Session, *, pause: bool = True
    ) -> dict[str, Any]:
        if pause:
            session.engine.pause()
        position = session.engine.checkpoint_position()
        return session.checkpoint_store.save(
            session_id=session.id,
            index=session.index,
            position=position,
            transport=session.transport,
        )

    def restore(
        self,
        checkpoint_envelope_or_path: dict[str, Any] | str,
        *,
        transport: Optional[str] = None,
        autoplay: bool = False,
    ) -> Session:
        """Validate a checkpoint against disk and create a restored session.

        The new session starts PAUSED at the checkpoint position. If the bag
        has changed on disk in any way (file hash, size, message content or
        order), :class:`SourceChangedError` is raised and nothing is created.
        """
        # Signature verification needs a store; use a scratch one bound to the
        # top-level state dir (id validation only applies to filename saves).
        scratch = CheckpointStore(self.settings.state_dir, self._key)
        if isinstance(checkpoint_envelope_or_path, dict):
            payload = scratch.load_verified(checkpoint_envelope_or_path)
        else:
            envelope_file = Path(checkpoint_envelope_or_path)
            import json

            payload = scratch.load_verified(
                json.loads(envelope_file.read_text(encoding="utf-8"))
            )

        fresh_index, payloads = self._restore_index(payload)
        position = payload["position"]

        if position.get("topic_filter"):
            unknown = sorted(
                set(position["topic_filter"]) - set(fresh_index.topic_names())
            )
            if unknown:
                raise ValueError(
                    f"checkpoint topic filter no longer valid: {unknown}"
                )

        transport = transport or payload.get("transport") or self.settings.transport
        session_id = uuid.uuid4().hex[:16]
        sink = self._build_sink(transport, session_id)
        cp_store = CheckpointStore(self.settings.state_dir / session_id, self._key)
        engine = ReplayEngine(
            fresh_index,
            sink,
            tick_seconds=self.settings.tick_seconds,
            topic_filter=position.get("topic_filter"),
            session_id=session_id,
            payloads=payloads,
        )
        engine.restore_position(position)
        if autoplay:
            engine.play()
        session = Session(
            id=session_id,
            index=fresh_index,
            engine=engine,
            sink=sink,
            transport=transport,
            checkpoint_store=cp_store,
            bag_dir=Path(fresh_index.uri),
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def _restore_index(self, payload: dict[str, Any]):
        bag_dir = Path(payload["bag"]["uri"])
        # The allow-list still applies; restored bags must live under a root.
        bagstore.resolve_bag_uri(str(bag_dir), self.settings.bag_roots)
        fresh_index, payloads = bagstore.index_bag_with_payloads(bag_dir)
        from .crypto import canonical_json

        summary = payload["bag"]
        current = fresh_index.digest_summary()
        for field_name in (
            "files",
            "message_index_sha256",
            "total_payload_bytes",
            "message_count",
            "starting_time_ns",
            "duration_ns",
            "storage_id",
            "topics",
        ):
            if canonical_json(current[field_name]) != canonical_json(summary[field_name]):
                raise SourceChangedError(
                    f"bag source changed since checkpoint: {field_name} differs"
                )
        return fresh_index, payloads
