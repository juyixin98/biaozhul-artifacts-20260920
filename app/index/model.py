"""索引模型：元数据与持久化索引的只读视图。"""

from dataclasses import dataclass

from app.core.dawg import Dawg


@dataclass(frozen=True)
class IndexMetadata:
    """索引元数据（不可变）。"""

    index_name: str
    schema_version: int
    allow_empty_word: bool
    source_fixture: str
    build_version: str
    word_count: int
    state_count: int
    edge_count: int

    @classmethod
    def from_storage(cls, raw: dict[str, str]) -> "IndexMetadata":
        return cls(
            index_name=raw.get("index_name", "unknown"),
            schema_version=int(raw.get("schema_version", "0")),
            allow_empty_word=raw.get("allow_empty_word", "False") == "True",
            source_fixture=raw.get("source_fixture", ""),
            build_version=raw.get("build_version", "unknown"),
            word_count=int(raw.get("word_count", "0")),
            state_count=int(raw.get("state_count", "0")),
            edge_count=int(raw.get("edge_count", "0")),
        )

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "index_name": self.index_name,
            "schema_version": self.schema_version,
            "allow_empty_word": self.allow_empty_word,
            "source_fixture": self.source_fixture,
            "build_version": self.build_version,
            "word_count": self.word_count,
            "state_count": self.state_count,
            "edge_count": self.edge_count,
        }


@dataclass(frozen=True)
class PersistedIndex:
    """持久化索引的只读视图：元数据 + 不可变自动机。"""

    metadata: IndexMetadata
    dawg: Dawg
