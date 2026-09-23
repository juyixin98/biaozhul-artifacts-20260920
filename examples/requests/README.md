# JSON 请求样例

这些文件可直接用 `curl` 发给 `python -m slang serve` 启动的服务。

先启动服务：

```bash
python -m slang serve --host 127.0.0.1 --port 8000
```

## 端点与样例

| 文件 | 端点 | 作用 |
|------|------|------|
| `01_compile.json` | `POST /compile` | 源码编译为模块（返回 hex 二进制 + 反汇编） |
| `02_verify.json` | `POST /verify` | 验证模块字节码 |
| `03_run.json` | `POST /run` | 验证通过后解释执行 main |
| `04_mutate.json` | `POST /mutate` | 生成单字节变异（默认只回元数据，不回二进制） |
| `05_compile_error.json` | `POST /compile` | 一个编译期错误样例（带源码行列） |

重新生成（会把当前编译器产出的二进制写进 02/03/04）：

```bash
python examples/requests/generate.py
```

## curl 示例

```bash
# 编译
curl -s -X POST http://127.0.0.1:8000/compile \
  -H 'Content-Type: application/json' \
  --data @examples/requests/01_compile.json

# 验证
curl -s -X POST http://127.0.0.1:8000/verify \
  -H 'Content-Type: application/json' \
  --data @examples/requests/02_verify.json

# 运行
curl -s -X POST http://127.0.0.1:8000/run \
  -H 'Content-Type: application/json' \
  --data @examples/requests/03_run.json

# 变异（只取前 50 条元数据）
curl -s -X POST http://127.0.0.1:8000/mutate \
  -H 'Content-Type: application/json' \
  --data @examples/requests/04_mutate.json

# 健康检查
curl -s http://127.0.0.1:8000/health
```

## 一次完整往返（管道）

```bash
BIN=$(curl -s -X POST http://127.0.0.1:8000/compile \
        -H 'Content-Type: application/json' \
        -d '{"source":"fn main(){ print 6*7; }"}' \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['module']['binary'])")

curl -s -X POST http://127.0.0.1:8000/run \
  -H 'Content-Type: application/json' \
  -d "{\"module\":{\"binary\":\"$BIN\",\"encoding\":\"hex\"}}"
# -> {"ok": true, "printed": ["42"], ...}
```

二进制模块支持 `hex`（默认）或 `base64`，在 `module.encoding` 中指定。
字段与错误结构的完整定义见根目录 `README.md` 与 `docs/BYTECODE.md`。
