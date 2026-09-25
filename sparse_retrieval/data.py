"""可复现的合成稀疏数据生成(不下载任何外部数据/模型)。

生成模型
--------
1. 若干"主题"各自是一个稀疏中心向量, 主题权重允许为负;
2. 每个文档 = 随机 1~3 个主题的混合 + 少量噪声维度;
3. 查询 = 某主题 + 噪声;
4. 以固定概率把部分权重取负, 用于覆盖负权重场景;
5. 通过 ``duplicate_prob`` 主动制造**重复维度**输入(建库前必须合并);
6. 通过 ``zero_docs`` 插入零向量文档。

所有随机性由种子控制, 同种子结果完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .vector import SparseVector

__all__ = ["SyntheticDataset", "generate_dataset"]


@dataclass(frozen=True)
class SyntheticDataset:
    dim: int
    docs: list[SparseVector]
    queries: list[SparseVector]
    topic_terms: list[np.ndarray]
    seed: int


def _sparse_random_terms(
    rng: np.random.Generator, dim: int, n_terms: int
) -> np.ndarray:
    return rng.choice(dim, size=n_terms, replace=False)


def generate_dataset(
    *,
    seed: int = 42,
    n_docs: int = 2000,
    n_queries: int = 50,
    dim: int = 1000,
    n_topics: int = 20,
    topic_terms: int = 12,
    doc_avg_nnz: int = 20,
    query_nnz: int = 10,
    negative_prob: float = 0.25,
    noise_term_prob: float = 0.35,
    duplicate_prob: float = 0.2,
    n_zero_docs: int = 5,
) -> SyntheticDataset:
    """生成一份合成数据集。

    Args:
        seed: 随机种子(可复现)。
        n_docs: 文档数(含零向量文档)。
        n_queries: 查询数。
        dim: 向量空间维度。
        n_topics: 主题数。
        topic_terms: 每个主题的核心维度数。
        doc_avg_nnz: 文档非零维度的期望规模(混合前)。
        query_nnz: 查询非零维度数(混合前)。
        negative_prob: 每个权重被取负的概率(覆盖负权重)。
        noise_term_prob: 每条权重走"噪声维度"而非主题维度的概率。
        duplicate_prob: 文档制造重复维度条目的概率。
        n_zero_docs: 插入的零向量文档数量。
    """
    rng = np.random.default_rng(seed)

    topics: list[tuple[np.ndarray, np.ndarray]] = []
    topic_term_ids: list[np.ndarray] = []
    for _ in range(n_topics):
        terms = _sparse_random_terms(rng, dim, topic_terms)
        weights = rng.normal(1.0, 0.3, size=topic_terms)
        signs = np.where(rng.random(topic_terms) < negative_prob, -1.0, 1.0)
        topics.append((terms, weights * signs))
        topic_term_ids.append(terms)

    def build_vector(
        n_mix_topics: int, n_terms: int, allow_duplicate: bool
    ) -> SparseVector:
        chosen = rng.choice(n_topics, size=n_mix_topics, replace=False)
        idx_list: list[int] = []
        val_list: list[float] = []
        terms_per_topic = max(1, n_terms // n_mix_topics)
        for topic_id in chosen:
            terms, weights = topics[int(topic_id)]
            take = min(terms_per_topic, terms.size)
            picked = rng.choice(terms.size, size=take, replace=False)
            for t, w in zip(terms[picked], weights[picked]):
                if rng.random() < noise_term_prob:
                    t = int(rng.integers(0, dim))
                idx_list.append(int(t))
                val_list.append(float(w) * float(rng.normal(1.0, 0.2)))
        # 纯噪声维度
        extra = max(0, n_terms - len(idx_list))
        if extra:
            # dim 很小时允许重复抽取(构造时会再次合并)
            noise = rng.choice(dim, size=extra, replace=extra > dim)
            for t in noise:
                idx_list.append(int(t))
                val_list.append(float(rng.normal(0.0, 0.5)))
        # 制造重复维度: 复制部分条目并给不同权重(可能异号 -> 抵消)
        if allow_duplicate and rng.random() < duplicate_prob and idx_list:
            n_dup = int(rng.integers(1, max(2, len(idx_list) // 3 + 1)))
            for j in rng.choice(len(idx_list), size=min(n_dup, len(idx_list)), replace=False):
                idx_list.append(idx_list[int(j)])
                val_list.append(float(rng.normal(0.0, 0.8)))
        return SparseVector.create(idx_list, val_list, dim)

    docs: list[SparseVector] = []
    for i in range(n_docs - n_zero_docs):
        n_mix = int(rng.integers(1, 4))
        nnz = int(rng.integers(doc_avg_nnz // 2, doc_avg_nnz * 2 + 1))
        docs.append(build_vector(n_mix, nnz, allow_duplicate=True))
    for _ in range(n_zero_docs):
        docs.append(SparseVector.create([], [], dim))
    rng.shuffle(docs)

    queries = [
        build_vector(n_mix_topics=1, n_terms=query_nnz, allow_duplicate=True)
        for _ in range(n_queries)
    ]

    return SyntheticDataset(
        dim=dim,
        docs=docs,
        queries=queries,
        topic_terms=topic_term_ids,
        seed=seed,
    )
