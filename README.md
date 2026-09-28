# bracket-index

大文本**带类型括号**结构索引：分块建摘要，支持局部编辑、括号匹配跳转和最短不平衡区间查询。

- 栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）
- 无外部服务、无生产账号；所有语料均为本地合成夹具

## 目录结构

```
bracket_index/
├── corpus/                 # 语料规范（职责一）
│   ├── spec.py             #   词法规范：括号类型、引号、转义符
│   └── fixtures.py         #   合成语料 + 手写参考答案（非被测代码生成）
├── kernel/                 # 挖掘内核（职责二）
│   ├── lexer.py            #   词法分析：引号/转义范围内不产生结构 token，可跨块传递词法状态
│   ├── summary.py          #   块摘要（有序残栈 + 错配）与保持类型次序的组合代数
│   └── oracle.py           #   独立全栈扫描参考实现（不用摘要机制，用于交叉校验）
├── index/                  # 索引与模型（职责三）
│   ├── store.py            #   SQLite 持久化：documents / chunks，参数化 SQL
│   ├── engine.py           #   分块建索引、局部编辑失效、偏移版本、平衡/匹配/区间查询
│   └── errors.py           #   带稳定类别字符串的领域错误
├── api/                    # 查询验证（职责四）
│   ├── app.py              #   FastAPI 工厂、请求标识中间件
│   ├── routes.py           #   路由：建文档、读状态、平衡、最短区间、匹配、编辑
│   ├── schemas.py          #   请求/响应模型（pydantic，含范围校验）
│   └── validation.py       #   领域错误 → HTTP 状态码/错误类别
├── config.py               # 环境变量配置
└── diagnostics.py          # 结构化决策日志（请求标识 + 脱敏指纹）
tests/                      # 独立组织的测试（51 个用例）
pytest.ini / requirements.txt
```

模块依赖方向：`api → index → kernel → corpus`，`diagnostics/config` 为横向公共模块。
`oracle` 不依赖 `index`，因此它能独立验证分块索引。

## 算法假设

1. **词法范围**：字符串字面量内的字符不是结构。进入引号后，其中的括号不产生 token；
   `\` 转义紧随其后的一个字符（`\"` 不闭合字符串，`\\` 是被转义的反斜杠）。
   未闭合的字符串会吞掉其后全部文本（确定性选择；见 `kernel/lexer.py`）。
2. **跨块词法状态**：每个块记录入口状态 `(当前引号或 None, 首字符是否被前一块尾部反斜杠转义)`，
   以及块尾连续奇数个反斜杠的"转义挂起"标志。引号或转义符落在块边界时结构仍然正确。
3. **块摘要**：每块扫描后保留三条有序序列：未匹配闭括号（按出现序）、未匹配开括号（按栈序）、
   类型错配事件（闭括号与栈顶类型不同，丢弃闭括号、保留开括号）。
4. **组合保持类型次序**：左块尾部开括号序列与右块前导闭括号序列仅在**同类型**时从栈顶配对，
   否则记为错配。因此各类型净计数相等但交叉错配的文本（如 `([)]`、`)(`）必被判定为不平衡——
   仅靠净计数无法证明平衡。
5. **局部编辑**：一次编辑只重新词法化受影响块（含起止块拼接后的新块）。
   若编辑改变了退出词法状态，则沿尾部逐块重扫，直到传播状态与某块存储入口状态重新一致即停止；
   之后的块摘要天然有效，永不全文重扫。块长度决定绝对偏移，尾部块仅重新编号。
6. **偏移版本**：文档带单调递增 `version`，编辑必须声明 `expected_version`；不匹配返回 409
   `STALE_VERSION`，从机制上杜绝基于旧版本偏移的错位写入。被拒绝的编辑不改动文档（事务回滚）。
7. **最短不平衡区间**：按首个缺陷给出最小证据区间——类型错配为"栈顶开括号→越界闭括号"闭区间，
   未匹配闭/开括号为其单字符区间。
8. 字符偏移为 Python 字符串下标（Unicode 码位）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/documents` | 建索引，body `{"text": "..."}` |
| GET | `/documents/{id}` | 文档版本与长度 |
| GET | `/documents/{id}/balance` | 是否平衡、类别、最短区间、各类缺陷计数 |
| GET | `/documents/{id}/unbalanced-interval` | 只取最短不平衡区间 |
| GET | `/documents/{id}/match?pos=N` | 匹配跳转：`MATCHED/match_pos`，或 `TYPE_MISMATCH/UNMATCHED_OPEN/UNMATCHED_CLOSE` |
| POST | `/documents/{id}/edits` | 局部替换 `{expected_version,start,end,replacement}` |

每个响应回带 `X-Request-ID`（可用同名请求头指定）。错误体形如
`{"error":{"category": "...", "message": "...", "request_id": "..."}}`，类别包括
`STALE_VERSION`(409)、`INVALID_RANGE`(422)、`NOT_A_BRACKET`(422)、`DOCUMENT_NOT_FOUND`(404)。

## 本地验证

```bash
pip install -r requirements.txt
python3 -m pytest                                  # 全部测试
python3 -m pytest --cov=bracket_index --cov-report=term   # 覆盖率（当前 97%）

# 真实服务冒烟
BRACKET_INDEX_DB_PATH=/tmp/bracket.db BRACKET_INDEX_CHUNK_SIZE=8 \
  python3 -m uvicorn bracket_index.api.app:app --port 8000
# 另开终端：
curl -s -X POST localhost:8000/documents -H 'Content-Type: application/json' \
  -d '{"text":"([)]"}'
curl -s localhost:8000/documents/1/unbalanced-interval
# 期望: {"balanced":false,"interval":{"start":1,"end":3,"category":"TYPE_MISMATCH"},...}
```

### 测试如何对照验收点

- `tests/test_summary.py`：`([)]`、`)(` 净计数为零但判不平衡；跨块类型错配；组合结合律。
- `tests/test_oracle_vs_index.py`：分块索引与**独立全栈扫描 oracle** 在全部手写夹具与
  60 个种子随机语料、1/3/8/64 四种块大小下，逐括号断言匹配位置与缺陷类别；
  参考答案来自 `corpus/fixtures.py` 的**手算结果**（token 位置、配对表、区间均手工列出）。
- `tests/test_quote_boundaries.py`：引号/转义跨块、引号插删使尾部路径失效且不重扫无关块。
- `tests/test_edits.py`：多块嵌套局部插删、连续编辑后与 oracle 一致、重扫块数受限、
  旧版本编辑返回 `STALE_VERSION` 且文档不变、偏移随插入正确平移。
- `tests/test_api.py`：断言具体 JSON、HTTP 状态、错误类别、请求标识，以及日志中**不出现**
  敏感正文（只出现 `len=.. sha256:..` 指纹）。

判断方式：全部用例绿灯；重点断言为具体位置/类别值（如区间 `[1,3)`、`match_pos=22`），
不是"接口能调用"。当前结果：**51 passed**（无未通过、无未运行；仅 1 条来自环境内
starlette/httpx 组合的第三方弃用警告，与本工程代码无关）。

## 依赖版本（本地验证通过）

| 包 | 版本 |
|---|---|
| Python | 3.12.3 |
| fastapi | 0.141.1 |
| starlette | 1.7.0 |
| pydantic | 2.13.5 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1（TestClient） |
| pytest | 9.1.1 |
| pytest-cov | 7.1.0 |

SQLite 使用标准库 `sqlite3`，无需额外安装。配置经环境变量：`BRACKET_INDEX_DB_PATH`、
`BRACKET_INDEX_CHUNK_SIZE`、`BRACKET_INDEX_LOG_LEVEL`。
