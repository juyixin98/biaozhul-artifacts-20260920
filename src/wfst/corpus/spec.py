"""语料规范层：合成对齐语料的 schema、校验与加载。

语料是显式版本化的本地 JSON 文件（合成夹具，无外部账号/业务数据）。
规范：

.. code-block:: json

  {
    "corpus_id": "char_corrections_v1",
    "version": "1.0.0",
    "description": "字符纠错 + 词形规则合成语料",
    "token_level": "char",
    "alignments": [
      {"input": "kat", "output": "cat", "count": 7, "tag": "spell"},
      {"input": "cat", "output": "cats", "count": 5, "tag": "morph"}
    ],
    "rules": [
      {"ilabel": "y", "olabel": "i", "weight_hint": 0.2, "kind": "sub"}
    ]
  }

校验失败抛 :class:`CorpusValidationError`，携带**全部**错误项（不止第一个），
调用方必须按失败处理，不允许静默当作空语料。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

EPSILON_LITERAL = "<eps>"


class Alignment(BaseModel):
    input: str = Field(min_length=1)
    output: str = Field(min_length=1)
    count: int = Field(default=1, ge=1)
    tag: str = Field(default="synthetic", min_length=1)

    @field_validator("input", "output")
    @classmethod
    def _no_epsilon_token(cls, v: str) -> str:
        if EPSILON_LITERAL in v:
            raise ValueError(f"语料符号不得包含保留字 {EPSILON_LITERAL!r}")
        return v


class RuleSpec(BaseModel):
    # sub 需要两侧非空；ins 的 ilabel、del 的 olabel 解释为 epsilon，
    # 允许留空（模型层按 kind 映射为 <eps>）。
    ilabel: str = Field(default="")
    olabel: str = Field(default="")
    weight_hint: float = Field(default=1.0)
    kind: str = Field(default="sub", pattern="^(sub|ins|del|identity)$")

    @field_validator("ilabel", "olabel")
    @classmethod
    def _labels(cls, v: str) -> str:
        if EPSILON_LITERAL in v:
            raise ValueError(f"规则符号不得包含保留字 {EPSILON_LITERAL!r}")
        return v

    @field_validator("weight_hint")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("weight_hint 必须是有限实数")
        return v

    def model_post_init(self, __context) -> None:
        if self.kind == "sub" and (not self.ilabel or not self.olabel):
            raise ValueError("sub 规则的 ilabel/olabel 均不能为空")
        if self.kind == "ins" and not self.olabel:
            raise ValueError("ins 规则必须给出 olabel")
        if self.kind == "del" and not self.ilabel:
            raise ValueError("del 规则必须给出 ilabel")
        if self.weight_hint < 0:
            raise ValueError("规则 weight_hint 不应为负（负代价环风险由核心层单独检测）")


class CorpusSpec(BaseModel):
    corpus_id: str = Field(min_length=1)
    version: str = Field(min_length=1, pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(default="")
    token_level: str = Field(default="char", pattern="^(char|word)$")
    alignments: list[Alignment] = Field(min_length=1)
    rules: list[RuleSpec] = Field(default_factory=list)
    # 只有带这些标签的对齐才参与字符/词形编辑操作挖掘；其余对齐（如
    # 词典义项标签 "lex"）只贡献词典映射，避免把词典输出差异误挖成编辑规则。
    edit_tags: list[str] = Field(default_factory=lambda: ["spell"])


class CorpusValidationError(ValueError):
    """语料文件不符合规范；``errors`` 列出全部失败项（失败类别明确）。"""

    def __init__(self, path: str | None, errors: list[str]):
        self.path = path
        self.errors = errors
        loc = f" {path}" if path else ""
        super().__init__(f"语料校验失败{loc}：\n  - " + "\n  - ".join(errors))


def parse_corpus(raw: dict, source: str | None = None) -> CorpusSpec:
    """解析并严格校验语料字典，失败时汇总所有错误后抛出。"""
    try:
        spec = CorpusSpec.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        raise CorpusValidationError(source, errors) from exc
    return spec


def load_corpus(path: str | Path) -> tuple[CorpusSpec, bytes]:
    """加载语料 JSON；返回 (规范对象, 原始字节)。原始字节用于计算指纹。

    :raises CorpusValidationError: JSON 非法或不符合规范。
    :raises FileNotFoundError: 文件不存在（明确的失败类别，不转成空语料）。
    """
    p = Path(path)
    raw_bytes = p.read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusValidationError(str(p), [f"JSON 解析失败：{exc}"]) from exc
    if not isinstance(raw, dict):
        raise CorpusValidationError(str(p), ["顶层必须是 JSON 对象"])
    return parse_corpus(raw, source=str(p)), raw_bytes


def corpus_fingerprint(raw_bytes: bytes) -> str:
    """语料内容指纹（sha256 前 16 位），写入日志关联运行身份。"""
    return hashlib.sha256(raw_bytes).hexdigest()[:16]
