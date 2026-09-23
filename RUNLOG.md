# 实际运行记录

环境：Python 3.12.3, Linux 6.8.0-90-generic x86_64, 日期 2026-09-23

## 1. 自动化测试

```
----------------------------------------------------------------------
Ran 92 tests in 11.169s

OK
```

## 2. 六个验收示例（优化前 AST / 优化前 IR / 优化后 IR 等价性）

```
PASS  01_unreachable_branch.l0
PASS  02_loop_constant.l0
PASS  03_confluence.l0
PASS  04_divzero.l0
PASS  05_side_effects.l0
PASS  06_undefined.l0
----------------------------------------
examples: 6 passed, 0 failed
```

## 3. 差分模糊（4 个种子，共 9000 随机程序）

```
seed 99: 3000 programs, 0 mismatches
seed 1: 2000 programs, 0 mismatches
seed 42: 2000 programs, 0 mismatches
seed 7777: 2000 programs, 0 mismatches
TOTAL 9000 programs in 93s, 0 mismatches
```

## 4. 示例 1 完整 optimize 输出（不可达分支裁剪）

```
== SCCP 常量 ==
  $t1.1        = 42
  x.1          = 40
  x.2          = 42

== 优化改写 ==
{
  "materialized_constants": [
    "x.1",
    "$t1.1",
    "x.2"
  ],
  "simplified_branches": [
    {
      "block": "entry",
      "condition": 1,
      "kept": "then.1",
      "dropped": "else.2"
    }
  ],
  "removed_blocks": [
    "else.2"
  ],
  "removed_edges": [
    [
      "entry",
      "else.2"
    ]
  ],
  "phi_pruned": [
    "z.2"
  ],
  "phi_to_copy": [
    "z.2"
  ]
}

== 优化后 IR ==
entry:            ; preds: -
  jmp then.1
then.1:            ; preds: entry
  print #42
  jmp endif.3
endif.3:            ; preds: then.1
  print #10
  exit

== 运行对比 ==
  AST 金标准   : OK  output=[42, 10]
  优化前 IR    : OK  output=[42, 10]
  优化后 IR    : OK  output=[42, 10]

  未优化IR==AST: True
  优化保持等价 : True
```

## 5. 示例 4 除零错误行为（优化前后一致）

```
== 运行对比 ==
  AST 金标准   : ERROR division-by-zero at (12, 5) output_before_error=[1, 2]
  优化前 IR    : ERROR division-by-zero at (12, 5) output_before_error=[1, 2]
  优化后 IR    : ERROR division-by-zero at (12, 5) output_before_error=[1, 2]

  未优化IR==AST: True
  优化保持等价 : True
```

## 6. JSON HTTP 服务真实响应

### GET /health
```
{
  "ok": true,
  "status": "alive"
}
```

### POST /analyze（合流反例，节选 sccp.lattice）
```
{
  "ok": true,
  "lattice": {
    "$t1.1": "bottom",
    "$t2.1": "bottom",
    "$t3.1": "bottom",
    "i.1": "const(1)",
    "i.2": "bottom",
    "i.3": "bottom",
    "k.1": "const(0)",
    "k.2": "bottom",
    "k.3": "bottom",
    "y.1": "const(40)",
    "y.2": "bottom",
    "y.3": "const(20)",
    "z.1": "const(7)",
    "z.2": "const(7)",
    "z.3": "const(7)"
  },
  "reachable_blocks": [
    "else.5",
    "else.8",
    "endif.6",
    "endif.9",
    "entry",
    "then.4",
    "then.7",
    "while.body.2",
    "while.cond.1",
    "while.end.3"
  ]
}
```

### POST /run（除零，结构化错误）
```
{
  "ok": true,
  "run": {
    "output": [
      1,
      2
    ],
    "output_text": "1\n2",
    "ok": false,
    "error_code": "division-by-zero",
    "error_message": "divide by zero",
    "location": [
      5,
      5
    ],
    "steps": 14
  }
}
```
