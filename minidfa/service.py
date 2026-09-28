"""服务编排层：把语料规范、挖掘内核、持久化串成可诊断的构建/查询流程。

每次构建分配 run_id，日志携带 run_id、输入指纹、版本与关键计算步骤，
使测试与运维能把日志关联到具体输入和运行身份。
"""

from __future__ import annotations

import platform
import time
import uuid
from dataclasses import dataclass

from . import __version__
from .automaton import MinimalDFA
from .builder import build_minimal_dfa
from .config import Settings, get_logger
from .corpus import InputMode, prepare_corpus
from .persistence import AutomatonStore

logger = get_logger()


@dataclass(frozen=True)
class BuildReport:
    run_id: str
    name: str
    fingerprint: str
    word_count: int
    state_count: int
    edge_count: int
    elapsed_ms: float


class DfaService:
    """应用服务：构建、持久化、查询命名自动机。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.store = AutomatonStore(self.settings.db_path)

    def close(self) -> None:
        self.store.close()

    def build(self, name: str, words: list[str], mode: InputMode = InputMode.STRICT) -> BuildReport:
        """校验语料 → 构建最小 DFA → 持久化。任一步失败按类别抛出，不吞异常。"""
        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        logger.info(
            "run=%s build start name=%r mode=%s raw_words=%d minidfa=%s python=%s",
            run_id, name, mode.value, len(words), __version__, platform.python_version(),
        )

        if len(words) > self.settings.max_words:
            from .errors import CorpusError

            raise CorpusError(
                f"corpus too large: {len(words)} words > max {self.settings.max_words}",
                detail={"word_count": len(words), "max_words": self.settings.max_words},
            )

        corpus = prepare_corpus(words, mode)
        logger.info(
            "run=%s corpus validated fingerprint=%s words=%d (strict order+unique)",
            run_id, corpus.fingerprint, corpus.size,
        )

        automaton = build_minimal_dfa(corpus.words)
        edge_count = len(automaton.edge_rows())
        logger.info(
            "run=%s minimized states=%d edges=%d words=%d",
            run_id, automaton.state_count, edge_count, len(automaton),
        )

        self.store.save(automaton, name, corpus.fingerprint)
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("run=%s persisted name=%r elapsed_ms=%.1f", run_id, name, elapsed)

        return BuildReport(
            run_id=run_id,
            name=name,
            fingerprint=corpus.fingerprint,
            word_count=corpus.size,
            state_count=automaton.state_count,
            edge_count=edge_count,
            elapsed_ms=elapsed,
        )

    def get(self, name: str) -> MinimalDFA:
        """加载（含引用校验）。不存在或损坏按类别抛出。"""
        return self.store.load(name)

    def contains(self, name: str, word: str) -> bool:
        return self.get(name).contains(word)

    def prefix_count(self, name: str, prefix: str) -> int:
        return self.get(name).prefix_count(prefix)

    def stats(self, name: str) -> dict[str, object]:
        automaton = self.get(name)
        return {
            "name": name,
            "state_count": automaton.state_count,
            "edge_count": len(automaton.edge_rows()),
            "word_count": len(automaton),
            "minidfa_version": __version__,
        }
