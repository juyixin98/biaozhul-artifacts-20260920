# manifest-selector — OCI 多架构清单选择服务（Rust + Axum）

纯后端 HTTP 服务，实现 OCI Image Index 风格的**多平台制品选择**：按
`os` / `architecture` / `variant` / `osVersion` / `osFeatures` 在清单树中
匹配目标子清单，校验每一步的内容摘要，并输出完整的**选择路径**。

核心约束：

- **绝不联网、绝不下载公网镜像**。内容只来自本地 `fixtures/` 目录或
  `/admin/...` 注入接口；描述符指向未存储的 blob 时返回明确错误。
- **歧义不靠文件顺序消歧**。多个不同 blob 满足完全相同（或同等优先）的
  平台条件时，返回 `409 ambiguous` 并列出全部候选，而不是取数组第一个。
- **每跳都做 SHA-256 验签**。内容与声称摘要不符返回 `422 digest_mismatch`，
  并给出实际摘要。

---

## 目录

- [依赖与环境](#依赖与环境)
- [启动命令](#启动命令)
- [生成演示数据](#生成演示数据)
- [HTTP 接口](#http-接口)
- [请求/响应样例](#请求响应样例)
- [匹配与歧义规则](#匹配与歧义规则)
- [关于“子清单环”的说明](#关于子清单环的说明)
- [自动化测试](#自动化测试)
- [实际运行结果](#实际运行结果)
- [项目结构](#项目结构)
- [未完成项 / 边界](#未完成项--边界)

---

## 依赖与环境

- Rust（开发使用 `cargo 1.98.1` / `rustc 1.98.1`，edition 2021）
- Python 3（仅用于重新生成演示 fixture，运行服务本身不需要）
- 无系统级 C 依赖；纯 Rust 加密实现（`sha2`）。

运行时/测试所需 crate 已在 `Cargo.lock` 锁定：

| crate | 版本 | 用途 |
| --- | --- | --- |
| axum | 0.7.9 | HTTP 路由与处理器 |
| tokio | 1.x | 异步运行时、TCP 监听 |
| serde / serde_json | 1.x | JSON 模型与规范化 |
| sha2 / hex | 0.10 / 0.4 | SHA-256 摘要 |
| tower | 0.5 | 并发限制层 |

无网络环境也可构建：依赖已在本机 cargo 缓存中，`cargo build --offline`
可用。

## 启动命令

```bash
# 1. 生成演示用本地 fixture（一次性；已附带生成结果，可跳过）
python3 scripts/gen_fixtures.py

# 2. 构建
cargo build --release

# 3. 启动（加载 fixtures/ 目录，监听 8080）
./target/release/manifest-selector --fixtures fixtures --listen 0.0.0.0:8080
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-l, --listen <ADDR:PORT>` | `0.0.0.0:8080` | 监听地址 |
| `-f, --fixtures <DIR>` | 空 | 启动时加载 `<DIR>/*/fixture.json` |
| `--max-concurrency <N>` | `256` | 同时在处理的请求上限 |
| `-h, --help` | | 帮助 |

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

## 生成演示数据

`fixtures/` 下每个子目录是一个仓库，`fixture.json` 列出 blob（规范化后的
OCI index/manifest JSON）与 tag。脚本按字节真实计算 sha256，**不手写摘要**，
序列化采用紧凑、键排序的规范形式（与 serde_json 默认输出一致）。

| 仓库 | 覆盖场景 |
| --- | --- |
| `demo-multiarch` | amd64 / arm v5、v6、v7 / arm64 v8 / windows 多版本；数组顺序刻意打乱 |
| `demo-ambiguous` | 两个不同 blob，平台元组完全相同 → 歧义 |
| `demo-nested` | index 嵌套 index，需走两层 |
| `demo-missing` | 描述符指向一个**未存储**的摘要 → missing_blob |

---

## HTTP 接口

### `POST /v1/select?reference=<引用>`

选择平台子清单。

引用格式：`repository[:tag][@digest]`，省略 tag 时为 `latest`；
同时给 `tag@digest` 时会校验 tag 解析结果与所钉摘要一致，否则 `400`。

请求体（JSON）：

```json
{
  "os": "linux",
  "architecture": "arm",
  "variant": "v7",
  "osVersion": "10.0.17763",
  "osFeatures": ["smp"]
}
```

- `os`、`architecture` 必填；其余可选。
- 字段名为 OCI 规范的 camelCase。

**成功 `200`**：返回选中清单与从根到叶的 `path`（见下方样例）。

**错误**（JSON 均含稳定 `code` 字段）：

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 400 | `bad_request` | 引用/摘要/请求体非法、tag 与所钉摘要不一致、缺 os/arch |
| 404 | `not_found` | 仓库/tag 不存在 |
| 404 | `no_match` | 索引中没有满足平台条件的子清单，返回 `examined` 列表 |
| 409 | `ambiguous` | 多个不同 blob 同等匹配，拒绝按文件顺序选择；返回 `candidates` |
| 409 | `manifest_cycle` | 遍历检测到清单引用环（纵深防御，见[环说明](#关于子清单环的说明)） |
| 422 | `missing_blob` | 描述符引用了本地不存在的 blob（不下载） |
| 422 | `digest_mismatch` | 内容摘要验签失败，返回 `claimed` 与 `actual` |
| 422 | `invalid_content` | 子 blob 既非合法 index 也非 manifest |

### 管理/测试接口（本地内容管理，非 registry 代理）

| 方法 路径 | 说明 |
| --- | --- |
| `GET /admin/repos` | 列出仓库、tag、blob 数 |
| `PUT /admin/blobs` | 写入 blob：`{repository, json}` 或 `{repository, rawBase64}`，按真实 sha256 存储 |
| `PUT /admin/blobs/claim` | **仅测试**：`{repository, claimedDigest, json}` 把内容存到任意摘要键下（制造验签失败） |
| `GET /admin/blobs/blob?repository=&digest=` | 取 blob（取时验签） |
| `PUT /admin/tags` | `{repository, tag, digest}` 打 tag |
| `PUT /admin/dangling-tag` | **仅测试**：制造“tag 指向错误摘要” |

---

## 请求/响应样例

以下用 `demo-multiarch`，其 `manifests` 数组顺序刻意把答案放到非首位。

### 1) 选择 linux/arm v7（不是数组第一个）

```bash
curl -s -X POST \
  "http://127.0.0.1:8080/v1/select?reference=demo-multiarch:latest" \
  -H 'content-type: application/json' \
  -d '{"os":"linux","architecture":"arm","variant":"v7"}'
```

```json
{
  "repository": "demo-multiarch",
  "resolvedTag": "latest",
  "rootDigest": "sha256:765b5e68…3b372",
  "requested": { "os": "linux", "architecture": "arm", "variant": "v7", "osVersion": null, "osFeatures": [] },
  "matchType": "index",
  "digest": "sha256:77b62adf…1b1e",
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "size": 402,
  "platform": { "architecture": "arm", "os": "linux", "variant": "v7" },
  "path": [
    { "digest": "sha256:765b5e68…3b372", "kind": "index" },
    { "digest": "sha256:77b62adf…1b1e", "kind": "manifest", "viaChildIndex": 3,
      "platform": { "architecture": "arm", "os": "linux", "variant": "v7" } }
  ]
}
```

`viaChildIndex: 3` 表明选中的是根索引 `manifests` 数组下标 3 的条目（数组
首位是 windows，第 2 个是 arm/v5），证明结果由匹配决定而非顺序。

### 2) 其它 ARM 变体

```bash
curl -s -X POST "$B/v1/select?reference=demo-multiarch:latest" \
  -H 'content-type: application/json' \
  -d '{"os":"linux","architecture":"arm","variant":"v5"}'
# -> platform.variant == "v5"

curl -s -X POST "$B/v1/select?reference=demo-multiarch:latest" \
  -H 'content-type: application/json' \
  -d '{"os":"linux","architecture":"arm64","variant":"v8"}'
# -> platform.variant == "v8"

# 不指定 variant：在 linux/arm 中优先最高变体 v7
curl -s -X POST "$B/v1/select?reference=demo-multiarch:latest" \
  -H 'content-type: application/json' -d '{"os":"linux","architecture":"arm"}'
# -> platform.variant == "v7"
```

### 3) 缺失平台

```bash
curl -s -i -X POST "$B/v1/select?reference=demo-multiarch:latest" \
  -H 'content-type: application/json' -d '{"os":"linux","architecture":"s390x"}'
# HTTP/1.1 404 Not Found
# {"code":"no_match",
#  "message":"no manifest found for os=\"linux\" architecture=\"s390x\" variant=<unset> …",
#  "examined":[ … 索引中出现过的全部 platform … ]}
```

### 4) 相同条件多候选 → 歧义

```bash
curl -s -i -X POST "$B/v1/select?reference=demo-ambiguous:latest" \
  -H 'content-type: application/json' \
  -d '{"os":"linux","architecture":"arm64","variant":"v8"}'
# HTTP/1.1 409 Conflict
# {"code":"ambiguous",
#  "message":"2 distinct manifests match … with equal preference; refusing to pick by file order",
#  "candidates":[ {…digest A…}, {…digest B…} ]}
```

调换数组中 A/B 的顺序（测试 `ambiguity_is_independent_of_descriptor_order`
已覆盖），结果仍然是 409，且两个 digest 都在 `candidates` 中。

### 5) 嵌套索引（index → index → manifest）

```bash
curl -s -X POST "$B/v1/select?reference=demo-nested:latest" \
  -H 'content-type: application/json' \
  -d '{"os":"linux","architecture":"arm","variant":"v7"}'
# path 的 kind 序列：["index","index","manifest"]
```

### 6) 引用缺失 blob（不下载）

```bash
curl -s -i -X POST "$B/v1/select?reference=demo-missing:latest" \
  -H 'content-type: application/json' -d '{"os":"linux","architecture":"amd64"}'
# HTTP/1.1 422
# {"code":"missing_blob",
#  "digest":"sha256:abab…abab",
#  "at":"sha256:cade…0e20#manifests[0]"}
```

### 7) 摘要被篡改

```bash
# 上传真实 blob 后，用 /blobs/claim 把同样字节放到错误摘要键下并打 tag，
# 再选择 —— 返回 422 digest_mismatch，附 claimed 与 actual。
# {"code":"digest_mismatch","claimed":"sha256:2222…","actual":"sha256:6534…"}
```

完整可复制脚本见 [`examples/requests.sh`](examples/requests.sh)。

---

## 匹配与歧义规则

1. **硬条件（hard match）**，对叶子描述符全部成立才进入候选：
   - `os`、`architecture` 精确相等；
   - 请求指定 `variant` 时必须精确相等（arm/v5、v6、v7、arm64/v8 互不兼容）；
     候选无 variant 而请求有 → 不匹配；
   - `osVersion`：请求指定则必须相等；
   - `osFeatures`：请求的每个特性候选都必须具备（候选可以是超集）。
2. **嵌套索引使用“粗兼容”剪枝**：父索引描述符是聚合/部分约束
   （例如只写 `linux/arm` 不写 variant），其未声明的字段不能剪掉携带该
   字段的后代子树；叶子仍按硬条件严格判定。
3. **优先打分（仅在硬匹配候选之间）**：variant 精确命中 > 未指定 variant 时
   取更高变体级别（v8>v7>v6>v5）> osVersion 贴近 > 更少多余 osFeatures。
4. **全局取最高分**，而不是遇到第一个匹配就返回。最高分上若存在
   **多个不同摘要的 blob**，判定为 `ambiguous`，错误里按规范化 JSON 排序后
   去重，列出全部候选及其路径。

## 关于“子清单环”

需要如实说明一个密码学事实：**在内容按摘要寻址且每跳验签的存储里，一个
真正可遍历的多节点清单环在不发生哈希碰撞的情况下是构造不出来的**。环的
“闭合边”引用的摘要要么指向一个缺失 blob（先报 `missing_blob`），要么其
字节验签失败（先报 `digest_mismatch`）。这是正确的安全属性——验签保证了
结构上的环无法被走到。

本项目据此做了两层处理：

- **遍历器仍内置环检测**（记录根到当前索引的摘要栈，重复即返回
  `409 manifest_cycle` 与环路径），作为纵深防御；通过测试钩子
  （`SelectOptions { verify_digests: false }`，仅测试可用）在单元测试
  `cycle_guard_rejects_self_referential_structure` 中直接验证；
- **HTTP 路径始终强制验签**，篡改的三子清单环在第一跳即被
  `422 digest_mismatch` 拦截（HTTP 测试
  `tampered_ring_is_stopped_by_digest_verification` 及 `examples/requests.sh`
  第 10 步实际演示）。

---

## 自动化测试

```bash
cargo test              # 全部 36 个测试
cargo clippy --all-targets   # lint（无警告）
cargo build --release   # 发布构建
```

测试构成：

- `tests/digest_test.rs`：摘要解析、SHA-256 已知向量、验签、不支持算法；
- `tests/select_test.rs`（14 个）：arm v5/v6/v7、arm64/v8、未指定 variant
  取最高、缺失平台、不存在的 variant、同条件两候选歧义、**调换顺序仍歧义**、
  嵌套索引全路径、缺失 blob、验签失败带 actual、环守卫、直接 manifest、
  tag@digest 不一致、osFeatures 超集匹配；
- `tests/api_test.rs`（13 个）：对真实 Axum router 的端到端测试，含 fixture
  加载、各错误码、admin 注入、篡改环、真实 TCP 监听冒烟；
- `tests/loader_test.rs`：fixture 加载与全部 tag 验签；
- 源码内单元测试：引用解析、base64、variant 排序。

## 实际运行结果

以下命令均在本机实际执行（Rust 1.98.1，Linux x86_64）：

- `cargo test`：**36 passed, 0 failed**（lib 4、select 14、api 13、digest 3、
  loader 2）。
- `cargo clippy --all-targets`：无警告。
- `cargo build --release`：成功。
- 用 release 二进制 + 生成的 fixture 实际通过 HTTP 跑过 10 个场景
  （arm v5/v6/v7、arm64 v8、未指定 variant、缺失平台、歧义、嵌套、缺失 blob、
  摘要篡改、篡改环），输出与本文档一致；可执行 `examples/requests.sh` 复现。
- 全程**未发起任何对外网络连接**；内容仅来自本地目录与本机管理接口。

## 项目结构

```
.
├── Cargo.toml / Cargo.lock      # 依赖与锁定版本
├── fixtures/                    # 本地内容仓库（脚本生成物，随仓库提供）
│   ├── demo-multiarch/
│   ├── demo-ambiguous/
│   ├── demo-nested/
│   └── demo-missing/
├── scripts/gen_fixtures.py      # 按真实摘要生成 fixture（不手写 digest）
├── examples/requests.sh         # 一键复现全部 HTTP 场景
└── src/
    ├── main.rs                  # 参数解析、启动、加载、监听
    ├── lib.rs
    ├── api.rs                   # Axum 路由与处理器
    ├── select.rs                # 遍历、硬匹配、打分、歧义、环守卫
    ├── model.rs                 # OCI index/manifest/descriptor/platform
    ├── digest.rs                # sha256 摘要与验签
    ├── store.rs                 # 本地内容寻址存储（每取必验）
    ├── reference.rs             # repo[:tag][@digest] 解析
    ├── loader.rs                # fixture 目录加载（含 base64）
    └── error.rs                 # 错误码 -> HTTP 映射
```

## 未完成项 / 边界

- **非 OCI/Docker 四种已知 mediaType 的清单**按形状探测，仍无法识别则归为
  `other` 并在被引用时返回 `invalid_content`；未实现 OCI artifact
  `subject`/referrers 语义。
- 选择不解析镜像 **config blob** 内的 OS/架构信息，仅依据描述符的
  `platform`（OCI 多平台索引的标准位置）。
- 存储为**进程内存**，重启后由 `--fixtures` 重新装载；未做持久化/鉴权，
  `/admin/*` 与 `/blobs/claim` 面向本地演示/测试，不应直接暴露到不可信网络。
- 仅实现 SHA-256 验签；遇到其它算法的描述符会解析但无法验证（返回验签失败）。
- 环的 `manifest_cycle` 代码路径在诚实内容下不可达（见上节），仅能通过测试
  钩子触发；真实 HTTP 面看到的是 `digest_mismatch` / `missing_blob`。
