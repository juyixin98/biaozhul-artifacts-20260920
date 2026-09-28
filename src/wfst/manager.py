"""摄取管线与运行期模型管理器。

把「语料文件 → 校验 → 挖掘 → SQLite 入库 → 编译 → 内存注册」串成一条
显式管线，每一步的结果都可复核；任何一步失败都向上抛出明确异常，
不产出半成品模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wfst.config import Settings
from wfst.corpus.mining import MiningReport, mine_corpus
from wfst.corpus.spec import (
    CorpusValidationError,
    corpus_fingerprint,
    load_corpus,
)
from wfst.index.registry import CompiledModel, compile_model
from wfst.index.store import Store
from wfst.query import ErrorCategory, QueryError


@dataclass(slots=True)
class IngestResult:
    model: CompiledModel
    fingerprint: str
    report: MiningReport


class ModelManager:
    """内存模型注册表 + SQLite 仓储。键为 (corpus_id, version)。"""

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self._models: dict[tuple[str, str], CompiledModel] = {}

    def ingest_file(self, path: str | Path) -> IngestResult:
        """加载并摄取一个语料 JSON 文件（失败抛 CorpusValidationError）。"""
        (spec, raw_bytes) = load_corpus(path)
        fingerprint = corpus_fingerprint(raw_bytes)
        report = mine_corpus(spec, smoothing=self.settings.smoothing_count)
        self.store.upsert_corpus(spec, raw_bytes, fingerprint)
        self.store.save_mining(report)
        model = compile_model(spec, report)
        self._models[(spec.corpus_id, spec.version)] = model
        return IngestResult(model=model, fingerprint=fingerprint, report=report)

    def register(self, spec, report: IngestResult | CompiledModel) -> None:
        model = report if isinstance(report, CompiledModel) else report.model
        self._models[(model.corpus_id, model.version)] = model

    def get(self, corpus_id: str, version: str | None = None) -> CompiledModel:
        if version is not None:
            model = self._models.get((corpus_id, version))
            if model is None:
                raise QueryError(
                    ErrorCategory.MODEL_NOT_FOUND,
                    f"模型 {corpus_id}@{version} 未加载",
                )
            return model
        versions = sorted(v for (cid, v) in self._models if cid == corpus_id)
        if not versions:
            raise QueryError(
                ErrorCategory.MODEL_NOT_FOUND,
                f"模型 {corpus_id} 未加载",
            )
        return self._models[(corpus_id, versions[-1])]

    def list_models(self) -> list[dict]:
        return [
            {
                "corpus_id": m.corpus_id,
                "version": m.version,
                "token_level": m.token_level,
                "alphabet_size": len(m.alphabet),
                "pipeline_states": m.pipeline.num_states,
                "pipeline_arcs": len(m.pipeline.arcs),
            }
            for m in sorted(
                self._models.values(), key=lambda m: (m.corpus_id, m.version)
            )
        ]

    def load_from_store(self, corpus_id: str, version: str | None = None) -> CompiledModel:
        """从 SQLite 中的原始语料重建模型（持久化后冷启动）。"""
        raw = self.store.get_corpus_raw(corpus_id, version)
        from wfst.corpus.spec import parse_corpus

        spec = parse_corpus(raw)
        report = mine_corpus(spec, smoothing=self.settings.smoothing_count)
        model = compile_model(spec, report)
        self._models[(spec.corpus_id, spec.version)] = model
        return model


__all__ = ["IngestResult", "ModelManager", "CorpusValidationError"]
