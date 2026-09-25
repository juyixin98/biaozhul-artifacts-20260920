# JSON API 参考

所有接口仅监听回环地址，请求/响应均为 JSON。错误响应统一为
`{"error": "..."}`。

| 方法 | 路径 | 说明 | 成功/失败状态码 |
|---|---|---|---|
| GET | `/healthz` | 存活检查 | 200 |
| GET | `/projects` | 列出已导入项目 | 200 |
| PUT | `/projects/{name}` | 从本地夹具目录导入项目 | 201 / 400 / 409 |
| POST | `/projects/{name}/build` | 按拓扑序执行（带指纹缓存） | 200；动作失败 422 |
| GET | `/projects/{name}/verify` | 独立重算验证证明链 | 完整 200；不完整 **409** |
| POST | `/projects/{name}/reproduce` | 隔离存储根干净重跑并比对摘要 | 可复现 200；否则 **409** |
| GET | `/projects/{name}/impact?path=&digest=` | 源变更影响面 | 200 / 400 |
| GET | `/projects/{name}/records` | 项目记录索引 | 200 |
| GET | `/projects/{name}/records/{action}` | 某动作的签名溯源记录 | 200 / 404 |
| GET | `/projects/{name}/graph` | 声明式动作图 + 记录绑定 | 200 |

路径参数 `name` 与动作 ID 限 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`。

## PUT /projects/{name}

请求：`{"src_dir": "/abs/path/to/fixture", "replace": false}`

- `src_dir` 必须是**绝对路径**的本地目录（这是显式提供夹具的入口，服务不上
  传、不联网）。目录内须含合法 `build.json`。
- 导入为整树复制（拒绝符号链接）；导入后立即静态校验（含 DAG 循环检测），
  校验失败回滚。
- 同名项目已存在时返回 409；`"replace": true` 可覆盖。

## POST /projects/{name}/build

响应（节选）：

```json
{
  "project": "shared-demo",
  "status": "ok",
  "commands_run": ["tools/concat.sh out/a.bundle src/shared.txt src/a.txt", "..."],
  "actions": [
    {"action_id": "compile_a", "status": "executed",
     "record_id": {"algo": "sha256", "hex": "..."},
     "input_fingerprint": {"algo": "sha256", "hex": "..."},
     "outputs": [{"path": "out/a.bundle", "digest": {"...": "..."}, "bytes": 103}],
     "command": "tools/concat.sh ..."}
  ]
}
```

- `status` 每动作取值：`executed`（真实执行夹具）、`cached`（输入指纹命中，
  未执行任何命令）、`failed`。
- `commands_run` 只包含本次真实派生的夹具命令；缓存命中时为空。

## GET /projects/{name}/verify

```json
{
  "project": "shared-demo",
  "complete": false,
  "nodes": {
    "compile_a": {"action_id": "compile_a", "complete": false, "tainted": true,
                  "codes": ["FINGERPRINT_MISMATCH", "SOURCE_DIGEST_MISMATCH"]},
    "link":      {"action_id": "link", "complete": false, "tainted": true,
                  "codes": ["UPSTREAM_CHAIN_BROKEN"]}
  },
  "findings": [
    {"action_id": "compile_a", "code": "SOURCE_DIGEST_MISMATCH",
     "message": "source \"src/a.txt\" changed: disk 6a5eff.. != record 1594c3.."}
  ]
}
```

错误码（`code`）：

| 类别 | 码 |
|---|---|
| 结构 | `MISSING_RECORD` `RECORD_UNREADABLE` `UNKNOWN_ACTION` `UNDECLARED_UPSTREAM` `MISSING_UPSTREAM` `MISSING_UPSTREAM_RECORD` `CYCLE_DETECTED` |
| 密码/内容 | `RECORD_HASH_MISMATCH` `SIGNATURE_INVALID` `SPEC_MISMATCH` `TOOL_DIGEST_MISMATCH` `SOURCE_DIGEST_MISMATCH` `UPSTREAM_DIGEST_MISMATCH` `OUTPUT_DIGEST_MISMATCH` `FINGERPRINT_MISMATCH` `BLOB_MISSING` |
| 传递 | `UPSTREAM_CHAIN_BROKEN` |

## POST /projects/{name}/reproduce

在 `<root>/repro/rerun-<时间戳>/` 下开一个**全新存储根**（独立空缓存、独立
work 树，项目树复制一份），全量执行后逐输出比对新旧摘要：

```json
{
  "project": "shared-demo",
  "reproduced": true,
  "commands_run": ["...3 条，全部真实执行（冷缓存）..."],
  "actions": [
    {"action_id": "link", "status": "reproduced",
     "outputs": [{"output": "out/final.txt",
                  "original_digest": "sha256:e3fa14..",
                  "rerun_digest": "sha256:e3fa14..", "reproduced": true}]}
  ]
}
```

## GET /projects/{name}/impact

查询参数（至少一个）：`path=src/shared.txt` 和/或
`digest=sha256:<hex>`。

```json
{
  "project": "shared-demo",
  "matched_sources": ["src/shared.txt"],
  "affected_actions": ["compile_a", "compile_b", "link"],
  "affected_outputs": [
    {"action_id": "compile_a", "output": "out/a.bundle", "digest": {"...": "..."}, "distance": 1},
    {"action_id": "link",      "output": "out/final.txt", "...": "...",        "distance": 2}
  ],
  "chains": [
    {"source_path": "src/shared.txt", "path": ["compile_a", "link"], "output": "out/final.txt"}
  ]
}
```

`distance=1` 表示该动作直接读取此源；沿上游消费边反向 BFS 得到传递闭包。
