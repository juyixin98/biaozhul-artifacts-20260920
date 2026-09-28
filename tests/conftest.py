import base64

import pytest

from lcs_batch.config import Settings
from lcs_batch.corpus import Document, encode_corpus
from lcs_batch.kernel import SuffixKernel


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "test_index.db"))


def make_kernel(contents: list[bytes], doc_ids: list[str] | None = None) -> SuffixKernel:
    doc_ids = doc_ids or [f"doc{i}" for i in range(len(contents))]
    corpus = encode_corpus(
        [Document(doc_id=doc_id, content=content) for doc_id, content in zip(doc_ids, contents)]
    )
    return SuffixKernel(corpus)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
