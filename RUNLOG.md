# 运行记录（如实记录）

- 环境：Linux 6.8.0-90-generic，Python **3.12.3**（仅标准库，无第三方依赖）
- 执行时间：2026-09-23
- 一键复现：`bash run_checks.sh`

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests
```

结果（最终）：

```
............................................................................
----------------------------------------------------------------------
Ran 76 tests in 0.561s

OK
```

（测试文件：`test_lexer.py`、`test_parser.py`、`test_inference.py`、
`test_eval.py`、`test_service.py`、`test_acceptance_matrix.py`。
服务测试会在临时端口真实启动 stdlib HTTP 服务并发请求。）

## 2. 验收点 1：恒等函数多次实例化

```bash
python3 -m miniml.cli infer examples/identity.mlml --no-trace
```

```
val id : forall a. a -> a
val a : int
val b : bool
val c : a -> a  (monomorphic: value restriction)
val d : int
- : a -> a
```

推导轨迹（节选；完整见 `python3 -m miniml.cli infer -` 对
`let id = fun x -> x in id (id 5)` 的输出）：

```
generalize  id : a -> a  =>  forall a. a -> a
instantiate variable 'id': forall a. a -> a  =>  a -> a  (fresh: t1)
instantiate variable 'id': forall a. a -> a  =>  a -> a  (fresh: t2)
unify       argument type: int ~ a
```

两次使用产生**两个不同的新鲜变量**（t1、t2），互不影响。

## 3. 验收点 2：递归类型被 occurs-check 拒绝

```bash
python3 -m miniml.cli infer examples/occurs_check.mlml
```

```
error[E004]: argument type: occurs check failed; infinite type: cannot construct the infinite type a = a -> b
--> examples/occurs_check.mlml:2:23
  |
2 | let loop = fun x -> x x ;;
  |                     - ^^^ argument type: occurs check failed; ...
```

退出码：1。`x x` 令 `a ~ a -> b`，`a` 出现在右侧，发生检查失败。

## 4. 验收点 3：可变引用不健全反例

```bash
python3 examples/demo_unsound.py --check
```

- **值限制 ON（默认，健全）**：推导阶段拒绝（E003），位置指向冲突表达式
  `f true` 的参数 `true`（`5:11`）；
- **值限制 OFF（故意保留的开关，不健全）**：推导接受，`f` 被错误泛化为
  `forall a. a -> a` 并分别在 `bool -> bool` 与 `int -> int` 实例化；
  随后 CBV 求值在**运行时崩溃**：

```
runtime panic: operator '+': expected int but got runtime value true at line 3
=> accepted by inference, but RUNTIME PANIC (E005)
CHECK OK: VR rejects the program; without VR inference accepts and evaluation panics.
```

## 5. JSON 服务实测

```bash
python3 -m miniml.service --port 8765 --quiet &
curl -s http://127.0.0.1:8765/health
for f in examples/requests/*.json; do
  ep=infer; case "$f" in *eval*) ep=eval;; esac
  curl -s -o /tmp/resp.json -w "%{http_code}\n" -X POST \
    http://127.0.0.1:8765/$ep -H 'Content-Type: application/json' --data @$f
done
```

实测 HTTP 状态码：

| 请求文件 | 端点 | 状态 |
|---|---|---|
| `infer_identity.json` | /infer | **200** |
| `infer_occurs_check.json` | /infer | **400**（E004，位置 1:23） |
| `infer_reference_rejected.json` | /infer | **400**（E003，位置 4:11） |
| `infer_reference_unsound.json` | /infer | **200**（VR 关闭，错误地接受） |
| `infer_reference_unsound.json` | /eval | **400**（E005 运行时 panic） |
| `infer_type_error.json`（`1 + true`） | /infer | **400**（E003，位置 1:5） |
| `eval_factorial.json` | /eval | **200**，`final_value = "720"` |

响应正文保存在 `examples/responses/`（unsound 的 /eval panic 为交互中即时
查看，未落盘；可用上面的 curl 复现）。

## 6. 其它示例

```bash
python3 -m miniml.cli eval examples/recursion.mlml --no-trace
python3 -m miniml.cli eval examples/references.mlml --no-trace
```

两者均推导 + 求值成功；`references.mlml` 最终值 `2`。

## 7. 未通过项 / 已知限制

- 无未通过的测试或验收项。
- 语言刻意不含 ADT、模式匹配、元组与多参数语法（多参数用柯里化）；
  相等运算为受限多态。这些是范围取舍而非缺陷，详见 README §7。
