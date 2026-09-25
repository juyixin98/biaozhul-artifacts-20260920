# patchd — 补丁上下文应用服务（纯后端）

`patchd` 是一个本地构建的 Go 工程服务，通过 JSON 接口**精确**应用统一 diff
（unified diff）的一个严格子集。它只做逐字节匹配，不做任何模糊/猜测匹配；
一批补丁**先整体验证、再整体发布**，任何一个补丁验证失败，工作目录都不会
被改动；发布中途失败会回滚已完成的更改。

- 纯 Go 标准库实现，无第三方依赖，无前端。
- 只监听本地回环地址，不连接任何云平台。
- 暂存缓存目录与被补丁的工作目录**物理分离**。

## 目录结构

```
cmd/patchd/            CLI / 服务入口（serve、apply、validate）
internal/diff/         严格统一 diff 子集解析器（错误带 diff 行号）
internal/apply/        精确应用器：上下文、行号、行范围、无换行校验
internal/paths/        路径安全：词法清理 + 逐段符号链接解析
internal/batch/        批次：先验证暂存，再原子发布，失败备份回滚
internal/server/       本地 HTTP JSON 接口
examples/              请求样例
test/run_fixtures.sh   端到端测试夹具（唯一的夹具命令）
```

## 构建

```bash
go build ./...
go vet ./...
gofmt -l .      # 应无输出
```

## 运行

### HTTP 服务

```bash
go run ./cmd/patchd serve --addr 127.0.0.1:8080 --cache-dir ./.cache
```

| 方法 & 路径        | 说明                          |
|--------------------|-------------------------------|
| `GET /healthz`     | 健康检查                      |
| `POST /v1/apply`   | 验证并（可选）应用一批补丁    |

请求体：

```json
{
  "workdir": "/abs/path/to/workdir",
  "dry_run": false,
  "patches": [
    { "encoding": "utf-8", "diff": "--- a/f\n+++ b/f\n@@ ..." }
  ]
}
```

`encoding` 省略或为 `utf-8`/`text` 时按 UTF-8 文本处理，也支持 `base64`。

响应（成功）：

```json
{
  "status": "applied",
  "results": [
    { "path": "f", "operation": "modify",
      "stats": { "Hunks": 1, "Added": 1, "Removed": 1, "Context": 2,
                 "Changed": true, "Existed": true, "WillExist": true } }
  ]
}
```

`dry_run: true` 时 `status` 为 `validated`，不写工作目录。

失败响应 HTTP 状态为 `422`（验证失败）或 `409`（发布失败，已回滚）：

```json
{
  "status": "failed",
  "error": { "code": "context_mismatch", "index": 0, "path": "f",
             "hunk": 1, "line": 7, "message": "context mismatch at file line 2" }
}
```

`line` 指向补丁文本中的具体行，便于定位。

### 命令行

```bash
# 仅验证（等价 dry_run）
go run ./cmd/patchd validate --workdir ./demo/workdir \
    --cache-dir ./.cache --request examples/request-modify.json

# 实际应用
go run ./cmd/patchd apply --workdir ./demo/workdir \
    --cache-dir ./.cache --request examples/request-batch.json
```

`workdir` 与 `dry_run` 也可以写在请求 JSON 里；不给 `--request` 则从标准输入
读取请求。

## 支持的 diff 子集

```
diff --git a/path b/path        （可选）
index <h>..<h> <mode>           （可选，忽略）
new file mode / deleted file mode（可选）
--- a/path | /dev/null [\t 时间戳]
+++ b/path | /dev/null [\t 时间戳]
@@ -oldStart,oldLines +newStart,newLines @@ [小节标题]
 上下文行（前导空格）
+新增行
-删除行
\ No newline at end of file
```

- 一份补丁文本可以包含多个文件段。
- 支持：新建文件、修改文件、删除文件（含空文件）、多 hunk、纯插入。
- **显式拒绝**：二进制补丁、重命名/拷贝（两侧路径不同）、模式位变更、
  畸形 hunk 头、hunk 行计数与头部声明不符。

## 精确性规则（无模糊匹配）

1. 每个上下文行、删除行都必须与目标文件在 hunk 头声明的行号处**逐字节相等**。
2. hunk 声明的旧侧范围必须落在文件内，且相邻 hunk 的范围不得重叠或倒序。
3. 每个 hunk 的新侧起始行号必须与前面 hunk/间隔推算出的行号严格一致。
4. 无末尾换行严格校验：
   - hunk 覆盖到文件末尾时，补丁中 `\ No newline at end of file` 标记的有/无
     必须与旧文件及新结果完全一致；
   - 该标记后不得再有任何行，也不得出现在文件中间。
   - 该校验与 `git apply` 的接受/拒绝行为一致（已交叉验证）。
5. 路径安全：
   - 拒绝绝对路径、`..` 越界、指向工作目录根本身的路径、含 NUL 的路径；
   - 对已存在的每一级路径组件做符号链接解析，链接目标越出工作目录即拒绝
     （包括最后一级组件）；
   - 发布前会再次校验路径，防止验证后被换成符号链接（TOCTOU）。
6. 同一批次内同一目标文件出现两次会被拒绝。

## 批次原子性

1. **验证 + 暂存**：依次解析、校验所有补丁，把生成的新内容写入与工作目录
   分离的缓存目录（模式 0600）。任何一步失败：删除已暂存文件，工作目录
   保持原样，直接返回错误。
2. **发布**：所有补丁都通过后才开始。按路径排序、逐个 `rename` 安装：
   - 修改/删除：先把原文件 `rename` 为同目录隐藏备份，再放入新文件；
   - 新建：先在同目录写临时文件，再 `rename` 到目标。
3. **回滚**：发布途中任何一步失败，逆序恢复已改文件（新建的删除、修改的
   用备份换回、删除的恢复），并移除本批次新建的空目录。验证失败则根本不会
   进入发布阶段。

夹具脚本通过对整个工作目录取「相对路径 + 权限位 + SHA-256」的排序指纹，
在每个失败用例前后比对，确保**逐字节不变**。

## 测试

```bash
go test ./...            # 单元测试
./test/run_fixtures.sh   # 构建 + go test + CLI/HTTP 端到端夹具
```

夹具覆盖：dry-run 不落盘、批次新建/修改/删除、无末尾换行、错误上下文、
重叠 hunk、路径穿越、批次中单个失败导致整批不发布、验证失败不残留目录、
HTTP 200/422 及失败后目录指纹不变。

## 限制（有意为之）

- 不做模糊匹配、不做行号偏移自动搜索；上下文或行号不符即失败。
- 不支持重命名、模式位、符号链接目标、二进制补丁。
- 不提供鉴权/多租户；服务只应绑定本地回环地址。
