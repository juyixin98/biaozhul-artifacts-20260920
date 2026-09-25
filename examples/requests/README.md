# 请求样例

先启动服务：

```bash
go run ../../cmd/cdag serve --cache-dir /tmp/cdag-cache --addr 127.0.0.1:8080
```

| 文件 | 说明 |
|---|---|
| `01-register-project.sh` | 注册项目（把 workdir 替换为你本机的绝对路径） |
| `02-validate.sh` | 只校验不持久化（在 examples/ 目录下执行，或调整 `@project.json` 路径） |
| `03-build-all.sh` | 全量构建 |
| `04-build-target.sh` | 只构建 package 目标及其依赖闭包 |
| `05-inspect.sh` | 列表 / 详情 / 健康检查 |
| `sample-build-response.json` | 一次真实构建响应样例（含 key、reason、changes） |

## 失效解释示例（修改源文件后的第二次构建）

```json
{
  "node_id": "normalize",
  "status": "built",
  "reason": "cache miss: input file \"src/message.txt\" content changed ...; dependency \"source\" output ...; running command",
  "changes": [
    {"type": "input_changed", "detail": "input file \"src/message.txt\" content changed (5c1d96efc3 -> 1fc8781ff6)"}
  ]
}
```

无关节点则返回 `"status": "cached"` 并附带 `cache hit: ...` 原因。
