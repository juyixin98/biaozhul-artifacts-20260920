# 关联规则提升度审计后端

基于已有频繁项集生成关联规则并审计置信度、lift、leverage 的 FastAPI 后端。
全部数据来自本地合成夹具，无需生产账号或真实业务数据。

## 模块划分

| 模块 | 职责 |
|---|---|
| `app/corpus/` | 语料规范：事务校验、事务内去重（与支持库一致的集合语义）、入库 |
| `app/mining/` | 挖掘内核：Apriori 频繁项集、规则生成（置信度/lift/leverage）、完备剪枝 |
| `app/index/` | 索引与模型：SQLite 存储层、Pydantic 请求/响应模型 |
| `app/validation/` | 查询验证：空前件/后件按范围拒绝、阈值范围检查、稳定失败类别 |
| `app/api/` | FastAPI 薄适配层：请求 ID 中间件、错误映射 |
| `app/service.py` | 编排层：API 与验证脚本共用，所有行为可无服务器测试 |
| `app/diagnostics.py` | 带 request_id 的结构化决策日志、敏感项脱敏 |
| `tests/` | 独立参考实现（Fraction 精确计算）+ 手算参考值 + 夹具 |
| `scripts/verify.py` | 端到端验证脚本，无法执行的检查单列 |

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 61 个测试
.venv/bin/python scripts/verify.py    # 端到端验证，退出码非零即失败
.venv/bin/uvicorn app.api.main:app --port 8000
```

## API 概览

```
POST /v1/corpora                          创建语料（内联事务表）
POST /v1/corpora/{id}/mine                挖掘频繁项集  {"min_support": 0.6}
POST /v1/corpora/{id}/rules               生成规则      {"min_confidence": 0.8, "min_lift": 1.2}
POST /v1/corpora/{id}/rules/evaluate      评估单条规则  {"antecedent": [...], "consequent": [...]}
GET  /health
```

错误响应统一为 `{"category", "detail", "request_id"}`，`category` 为稳定
机器可读类别（如 `EMPTY_ANTECEDENT`、`UNDEFINED_ZERO_DENOMINATOR` 对应的
`undecidable` 决策见日志）。指标分母为零时输出 `null`（未定义），不为 0。

## 边界语义

见 [docs/semantics.md](docs/semantics.md)：零分母未定义、空侧拒绝、
事务内去重、高置信度非因果、样本量/罕见事件警示、剪枝完备性、诊断与脱敏。

## 依赖

运行时依赖固定于 `requirements.txt`（fastapi / uvicorn / pydantic），
测试依赖固定于 `requirements-dev.txt`（pytest / httpx）。
