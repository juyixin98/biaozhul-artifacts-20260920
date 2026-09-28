"""索引编排服务：把语料规范、挖掘内核、持久化串起来。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app import __version__
from app.core.dawg import Dawg, DawgBuilder
from app.corpus.fixtures import CorpusFixture, get_fixture
from app.corpus.spec import CorpusSpec, NormalizedCorpus
from app.index.errors import IndexError
from app.index.model import IndexMetadata, PersistedIndex
from app.index.repository import SQLiteIndexRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuildReport:
    """构建报告（诊断与日志用）。"""

    index_name: str
    metadata: IndexMetadata
    input_count: int
    unique_count: int
    duplicate_count: int
    merge_count: int
    raw_states_created: int
    sorted_by_service: bool
    steps: tuple[str, ...]

    def summary_lines(self) -> list[str]:
        return [
            f"index={self.index_name}",
            f"input_words={self.input_count}",
            f"unique_words={self.unique_count}",
            f"duplicates_removed={self.duplicate_count}",
            f"raw_states_created={self.raw_states_created}",
            f"states_after_minimize={self.metadata.state_count}",
            f"merges={self.merge_count}",
            f"sorted_by_service={self.sorted_by_service}",
        ]


class IndexService:
    """构建、持久化、加载与查询索引的应用服务。"""

    def __init__(self, repository: SQLiteIndexRepository) -> None:
        self._repository = repository

    # ---- 构建 -------------------------------------------------------------

    def build_from_words(
        self,
        words: list[str] | tuple[str, ...],
        *,
        index_name: str,
        spec: CorpusSpec | None = None,
        source_fixture: str | None = None,
    ) -> tuple[Dawg, BuildReport]:
        """规范化 → 挖掘最小 DAWG → 引用校验 → 持久化。"""
        spec = spec or CorpusSpec()
        normalized: NormalizedCorpus = spec.normalize(list(words))
        logger.info(
            "build start index=%s input=%d unique=%d duplicates=%d sort_first=%s",
            index_name,
            normalized.input_count,
            normalized.unique_count,
            normalized.duplicate_count,
            spec.sort_first,
        )

        builder = DawgBuilder()
        for word in normalized.words:
            builder.add(word)
        dawg = builder.build()

        bundle = self._repository.save(
            dawg,
            index_name=index_name,
            allow_empty_word=spec.allow_empty_word,
            source_fixture=source_fixture,
            build_version=__version__,
        )
        metadata = IndexMetadata.from_storage(bundle.metadata)
        report = BuildReport(
            index_name=index_name,
            metadata=metadata,
            input_count=normalized.input_count,
            unique_count=normalized.unique_count,
            duplicate_count=normalized.duplicate_count,
            merge_count=dawg.stats.merge_count,
            raw_states_created=dawg.stats.raw_states_created,
            sorted_by_service=normalized.was_sorted_by_us,
            steps=dawg.stats.steps,
        )
        for line in report.summary_lines():
            logger.info("build %s", line)
        return dawg, report

    def build_from_fixture(
        self,
        fixture_name: str,
        *,
        index_name: str | None = None,
        spec: CorpusSpec | None = None,
    ) -> tuple[Dawg, BuildReport]:
        fixture: CorpusFixture = get_fixture(fixture_name)
        name = index_name or fixture.name
        return self.build_from_words(
            fixture.as_list(),
            index_name=name,
            spec=spec,
            source_fixture=fixture.name,
        )

    # ---- 加载 -------------------------------------------------------------

    def load(self) -> PersistedIndex:
        bundle = self._repository.load_bundle()
        metadata = IndexMetadata.from_storage(bundle.metadata)
        dawg = self._repository.load_dawg()
        if metadata.word_count != dawg.total_words():
            msg = (
                f"元数据词数 {metadata.word_count} 与根状态计数 "
                f"{dawg.total_words()} 不一致，拒绝加载"
            )
            raise IndexError(msg)
        return PersistedIndex(metadata=metadata, dawg=dawg)

    def is_built(self) -> bool:
        return self._repository.exists()
