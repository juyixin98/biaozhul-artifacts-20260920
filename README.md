# 构建输入溯源（Build Input Provenance，bis）

纯后端、本地化的**声明式构建输入溯源**服务。它为每个制品建立「**源文件摘要 +
工具摘要 + 上游制品摘要**」的输入来源图，支持：

- 查询某个源码变更会影响哪些输出（传递闭包 + 依赖链）；
- 独立重读磁盘、重算摘要，检测**缺失**、**篡改**和**伪造循环**的证明链；
- 区分两个正交性质：**溯源完整性**（链是否真实未被破坏）与**构建可复现性**
  （干净环境重算是否得到字节相同的输出）。

无前端、无云平台连接。服务默认只监听 `127.0.0.1`，**只执行项目 `build.json`
中显式声明的夹具命令**；缓存（不可变、内容寻址）与工作目录（一次性、用完即
删）物理分离。

---

## 1. 威胁模型与设计

### 1.1 动作在隔离工作目录中执行

每个动作执行时获得一个全新的一次性目录 `work/<run>-<action>/`：

```
work/<run>-<action>/
├── src/shared.txt, src/a.txt ...   # 声明的源文件（按声明路径复制，只读）
├── in/<本地名>/<上游输出路径>       # 上游制品（从不可变缓存物化，只读）
├── out/...                         # 动作产物（声明的输出）
└── tmp/                            # 该动作私有的 TMPDIR
```

- 源、输出路径必须是项目内的干净相对路径，`in/` 为保留前缀，禁止 `..` 越界；
  路径解析带符号链接 containment 检查（见 `internal/safeio`）。
- 工具必须是项目目录内的常规可执行文件，按绝对路径执行；`PATH` 固定，
  环境变量用白名单（不继承调用方环境，不泄露凭据）。
- 动作结束后工作目录整体删除；产物按 SHA-256 移入不可变缓存（`0444`）。

### 1.2 每个制品的溯源记录

每个动作执行产出一条签名记录（`internal/provenance`）：

| 字段 | 内容 |
|---|---|
| `tool` | 工具相对路径、参数、**工具可执行文件 SHA-256** |
| `sources[]` | 每个声明源文件的路径与 **SHA-256** |
| `upstreams[]` | 每个上游制品的动作 ID、输出路径与 **SHA-256** |
| `outputs[]` | 每个输出的路径、SHA-256、字节数 |
| `input_fingerprint` | 对「项目+动作+工具+源+上游」整体的确定性指纹（顺序无关），同时作为缓存键 |
| `sig` | 对规范 JSON 主体的 **HMAC-SHA256** 签名（服务密钥，0600） |

记录 ID = 规范主体的 SHA-256，因此文件名与内容绑定；改主体即破坏
`RECORD_HASH_MISMATCH`，重签也无法改变记录 ID 绑定与跨记录摘要绑定。

### 1.3 验证器独立重算，不信任构建报告

`GET /verify`（`internal/verify`）对每个动作**重新从磁盘读取并计算**：工具
摘要、源摘要、输出缓存 blob 摘要、记录 HMAC、记录 ID、输入指纹，并做结构检查。
关键的防伪造点：

1. **直接改记录文件不重签** → `SIGNATURE_INVALID` + `RECORD_HASH_MISMATCH`。
2. **持有密钥者改链接再重签**：签名通过、ID 自洽，但子记录里写的上游摘要与
   **上游记录自己声明的输出摘要**交叉比对不一致 → `UPSTREAM_DIGEST_MISMATCH`。
   攻击者无法只改一条记录伪造来源。
3. **伪造循环**（在记录里给本来无环的图加回边并重签）→ 记录图上 DFS 检测后沿
   染色，环上所有动作 `CYCLE_DETECTED`，全部不完整。
4. **缺失**：索引缺记录 `MISSING_RECORD`、blob 被删 `BLOB_MISSING`、声明的上游
   无记录 `MISSING_UPSTREAM_RECORD`。
5. **传递性**：任一直接错误沿消费边闭包传播，下游标 `tainted` 并附
   `UPSTREAM_CHAIN_BROKEN`。完整链 = 本节点无错且全部上游完整（不动点计算）。

### 1.4 完整性 ≠ 可复现性（两者分别报告）

- **完整溯源**：输出到源/工具的签名链真实无损。
- **可复现**：在**全新隔离存储根**（空缓存、空工作树、项目树复制一份）中重跑，
  每个输出摘要与原记录一致。

二者正交，测试中均有实证：

| 情形 | 溯源完整 | 可复现 |
|---|---|---|
| 确定性夹具（shared-demo） | ✅ | ✅ |
| 每次嵌入 UUID 的非确定性夹具（nondet-demo） | ✅ | ❌ |
| 删除缓存 blob 后 | ❌（BLOB_MISSING） | ✅（干净重算仍字节相同） |

---

## 2. 目录结构

```
cmd/bis/               服务入口（HTTP/JSON，默认 127.0.0.1:8080）
internal/
  digest/              SHA-256 摘要
  safeio/              路径 containment（防 .. 与符号链接逃逸）
  spec/                build.json 模型、静态校验、拓扑排序、循环检测
  provenance/          溯源记录、规范序列化、HMAC 签名、输入指纹
  store/               项目树 / 不可变 blob 缓存 / 一次性 work / 记录与索引
  builder/             动作执行、输入物化、缓存命中、记录生成
  verify/              独立重算验证、篡改/缺失/循环分类、传递完整性
  impact/              源变更影响面（反向闭包、距离、示例链）
  repro/               隔离存储根中的可复现性重算
  api/                 JSON HTTP 接口
  testutil/            测试夹具（显式提供的 shell 工具）
examples/              两个随仓交付的夹具工程
scripts/live-demo.sh   对运行中服务的实机演练脚本
docs/                  API 参考、样例请求、运行记录
```

## 3. 快速开始

前置：Go 1.22+、`/bin/sh`（夹具使用）。

```bash
go build -o bin/bis ./cmd/bis
./bin/bis -addr 127.0.0.1:8080 -root ./.bis -timeout 15s
```

导入并构建随仓示例（菱形共享依赖）：

```bash
curl -sS -X PUT  localhost:8080/projects/shared-demo \
  -H 'Content-Type: application/json' \
  -d "{\"src_dir\":\"$PWD/examples/shared-demo\"}"
curl -sS -X POST localhost:8080/projects/shared-demo/build
```

其余请求见 **[docs/API.md](docs/API.md)** 与 **[docs/REQUESTS.md](docs/REQUESTS.md)**；
一条命令跑完整个实机流程：

```bash
BASE=http://127.0.0.1:8080 OUT=logs/live ./scripts/live-demo.sh
```

## 4. build.json 声明格式

```json
{
  "name": "shared-demo",
  "actions": [
    {
      "id": "compile_a",
      "tool": "tools/concat.sh",
      "args": ["out/a.bundle", "src/shared.txt", "src/a.txt"],
      "sources": ["src/shared.txt", "src/a.txt"],
      "outputs": ["out/a.bundle"]
    },
    {
      "id": "link",
      "tool": "tools/concat.sh",
      "args": ["out/final.txt", "in/a/out/a.bundle", "in/b/out/b.bundle"],
      "upstream": {"a": "compile_a", "b": "compile_b"},
      "outputs": ["out/final.txt"]
    }
  ]
}
```

- `tool`：项目内可执行文件（唯一被执行的东西）；`args` 原样传给它。
- `sources` / `outputs`：项目内干净相对路径；工作目录即 CWD。
- `upstream`：`本地名 -> 上游动作 ID`；上游全部输出物化到 `in/<本地名>/` 下。
- 动作图必须是 DAG（导入即校验，循环被拒绝）；输出不得跨动作重复声明。

## 5. 自动化测试

```bash
go test ./...            # 全部用例
go test -race ./...      # 竞态检测
go test ./internal/verify -v   # 篡改/伪造/缺失/循环分类明细
```

测试自行生成全部夹具（`internal/testutil`）：确定性拼接工具、每次产生新 UUID
的非确定工具、恒失败工具。覆盖：菱形共享依赖构建、增量缓存与源变更失效、
工作目录与缓存分离、HMAC 签名与内容 ID、未重签篡改、**持密钥重签伪造链接**、
**记录图伪造循环**、blob/记录缺失的传递污染、影响面分支边界、隔离重算、
以及完整性/可复现性的两组正交组合。

实际命令、输出与 HTTP 状态码的运行记录见 **[docs/RUNLOG.md](docs/RUNLOG.md)**。

## 6. 安全边界

- 只监听回环地址；不发起任何出站网络连接。
- 只执行 `build.json` 显式声明、且位于项目目录内的工具；无 shell 拼接。
- 项目导入拒绝符号链接；所有声明路径经 containment 校验。
- 夹具环境最小化（固定 `PATH`/`LANG`/私有 `TMPDIR`），单动作超时可配。
- 说明：HMAC 用于证明「记录由本服务签发且未被篡改」，它不是对持密钥者的防内
  鬼机制；防「持密钥伪造来源」依赖第 1.3 节的跨记录摘要绑定（测试
  `TestReSignedForgedLinkCaughtByCrossRecordBinding` 实证）。
