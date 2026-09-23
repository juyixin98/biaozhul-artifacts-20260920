# 运行记录（RUNLOG）

环境：Python 3.12.3，Linux，无第三方依赖（仅标准库）。
以下所有输出均为实际运行所得。

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests
```

结果：

```
Ran 63 tests in 0.56s
OK
```

测试分布：

- `tests/test_parser.py`：词法位置、注释、运算符、优先级、应用结合、
  顶层/行内 let 区分、参数与结果标注、ref 标注、错误 span。
- `tests/test_inference.py`：
  - 恒等函数多次独立实例化（int / bool / 自身应用）；
  - 一般化与实例化轨迹；
  - occurs-check 拒绝 `fun f -> f f`（含 naive 模式同样拒绝）；
  - 值限制拒绝引用多态滥用；naive 模式接受但运行期崩溃；
  - 高阶多态、递归（fact）、if/算术/比较、标注校验、
    冲突表达式行列定位。
- `tests/test_eval.py`：算术、闭包与遮蔽、递归 fib、可变单元累加、
  除零、naive 程序在算术处的运行期类型崩溃。
- `tests/test_server.py`：在真实本地端口起停 HTTP 服务，覆盖
  `/health`、`/analyze` 成功与 400、naive 模式、坏请求与 404。

## 2. 验收一：恒等函数多次实例化

```bash
python3 -m tinyinfer.cli infer examples/identity.tml
```

```
最终表达式类型: int
顶层绑定:
  id : forall 'a. 'a -> 'a
  a : int
  b : bool
  c : int
求值结果: 8
```

`--trace` 关键步骤（完整 31 步见实际运行）：

```
5. 一般化   一般化 id : forall 'a. 'a -> 'a
8. 合一     统一 'a 与 int：'a := int（应用 id：函数参数类型）   [6:14]
14. 合一    统一 'a 与 bool：'a := bool（应用 id：函数参数类型） [7:14]
18. 实例化  使用变量 id：... 实例化为 'a -> 'a                  [8:11]
19. 实例化  使用变量 id：... 实例化为 'a -> 'a                  [8:14]
```

第 8 步与第 14 步对同一个方案 `∀'a. 'a -> 'a` 做了两次相互独立的
实例化（分别落到 int、bool），证明 let 多态生效。

## 3. 验收二：递归类型被 occurs-check 拒绝

```bash
python3 -m tinyinfer.cli infer examples/recursive_type_reject.tml
# 退出码 1
```

```
[OccursError] occurs-check 失败：类型变量 'a 出现在 'a -> 'b 中，
无法构造递归（无限）类型  (4:23)
    4 | let loop = fun f -> f f
      |                       ^
```

同一程序加 `--naive` 仍被拒绝（occurs-check 与值限制无关）。

## 4. 验收三：可变引用反例

### 4.1 默认（值限制开启）—— 静态拒绝，退出码 1

```bash
python3 -m tinyinfer.cli infer examples/ref_unsoundness.tml
```

```
[UnifyError] 类型冲突：期望 bool，实际得到 int
（运算符 + 的左操作数）  (18:3)
   18 |   deref r + 1
      |   ^^^^^^^
```

### 4.2 关闭值限制（naive W）—— 检查通过、运行期崩溃，退出码 0

```bash
python3 -m tinyinfer.cli infer examples/ref_unsoundness.tml --naive
```

```
[求值期错误 RuntimeFailure] 算术运算 + 运行期收到非 int：
bool(True) + 1 —— 这正是 naive 多态放过的不健全程序的崩溃点
   18 |   deref r + 1
      |   ^^^^^^^^^^^
最终表达式类型: int
顶层绑定:
  r : forall 'a. 'a ref
```

对照清晰：naive W 把 `r` 一般化成 `∀'a. 'a ref` 导致类型系统不健全；
值限制让 `r` 保持单态，在检查阶段就拒绝该程序。

## 5. 其余示例

```bash
python3 -m tinyinfer.cli infer examples/polymorphic_recursion.tml
# twice : forall 'a. ('a -> 'a) -> 'a -> 'a；求值结果: 132

python3 -m tinyinfer.cli infer examples/mutable_state_sound.tml
# 单态 int ref 与递归结合；求值结果: 5050
```

## 6. HTTP 服务实测

启动：`python3 -m tinyinfer.server --port 8011`

```bash
curl -s http://127.0.0.1:8011/health
# {"ok": true, "service": "tinyinfer", "version": "1.0.0"}

curl -s -X POST .../analyze -d '{"source":"let id = fun x -> x in id 1"}'
# {"ok": true, "type": "int", "value": "1",
#  "bindings": [{"name": "id", "scheme": "forall 'a. 'a -> 'a"}], ...}
```

四个 `examples/request_*.json` 样例实测 HTTP 状态：

| 样例 | 状态 | 结果 |
|---|---|---|
| request_identity.json | 200 | type=int, value=42 |
| request_recursive_type.json | 400 | OccursError |
| request_ref_sound.json | 400 | UnifyError（值限制） |
| request_ref_naive.json | 200 | type=int，但 eval_error 为 bool+1 崩溃 |

## 7. 一键复现

```bash
bash run_acceptance.sh
# 结果：PASS=14 FAIL=0
```

## 8. 已知边界与未做项（如实记录）

- 语言为教学级核心：不含代数数据类型、模式匹配、多态递归（无标注）、
  元组/记录；相等/比较仅定义在 `int` 上。这些不影响三项验收。
- `new_ref` 是为演示"多态 + 可变引用"不健全性而提供的空引用原语；
  常规代码用 `ref v` 即可。
- 类型方案里若仍含未解析变量（被值限制保留的待定单态），错误信息
  以渲染后的 `'a/'b` 展示；内部替换变量名 `?n` 不出现在用户输出中。
- 未实现前端（按要求）。
