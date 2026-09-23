"""内置合约模型夹具（fixtures）。均可直接作为 /check 的 model 字段提交。

整数语义为 64 位有符号补码（与检查器一致）。默认不变量为
「每个 int 余额非负」且「所有 int 余额之和（模 2^64）等于初始总额」。
"""

from __future__ import annotations

INT_MAX = 2**63 - 1

# 安全转账：成对扣款/加款（守恒）、有余额守卫（非负）、带布尔重入锁。
# 在任意有界深度都不应出现反例。
SAFE_TRANSFER = {
    "name": "safe_transfer",
    "state_vars": [
        {"name": "alice", "type": "int", "init": 100},
        {"name": "bob", "type": "int", "init": 50},
        {"name": "locked", "type": "bool", "init": False},
    ],
    "actions": [
        {
            "name": "transfer",
            "params": [{"name": "amount", "type": "int", "min": 0, "max": 1000}],
            "guards": [
                {"expr": {"t": "cmp", "op": ">=",
                          "lhs": {"t": "var", "name": "alice"},
                          "rhs": {"t": "var", "name": "amount"}}},
                {"expr": {"t": "cmp", "op": "==",
                          "lhs": {"t": "var", "name": "locked"},
                          "rhs": {"t": "bool", "value": False}}},
            ],
            "effects": [
                {"kind": "assign", "target": "alice",
                 "expr": {"t": "arith", "op": "-",
                          "args": [{"t": "var", "name": "alice"},
                                   {"t": "var", "name": "amount"}]}},
                {"kind": "assign", "target": "bob",
                 "expr": {"t": "arith", "op": "+",
                          "args": [{"t": "var", "name": "bob"},
                                   {"t": "var", "name": "amount"}]}},
                {"kind": "lock", "target": "locked"},
            ],
        },
        {
            "name": "unlock",
            "params": [],
            "guards": [{"expr": {"t": "var", "name": "locked"}}],
            "effects": [{"kind": "unlock", "target": "locked"}],
        },
    ],
}

# 漏扣余额：withdraw 只加收款方余额、不扣付款方 —— 一步即破坏总额守恒
# （非负性仍满足，借此可以验证守恒检查确实独立生效）。
MISSING_DEBIT = {
    "name": "missing_debit",
    "state_vars": [
        {"name": "treasury", "type": "int", "init": 100},
        {"name": "attacker", "type": "int", "init": 0},
    ],
    "actions": [
        {
            "name": "withdraw",
            "params": [{"name": "amount", "type": "int", "min": 1, "max": 1000}],
            "guards": [],  # 没有任何守卫
            "effects": [
                {"kind": "assign", "target": "attacker",
                 "expr": {"t": "arith", "op": "+",
                          "args": [{"t": "var", "name": "attacker"},
                                   {"t": "var", "name": "amount"}]}},
                # BUG: 缺少 treasury = treasury - amount
            ],
        },
    ],
}

# 64 位溢出（带看似正确的余额守卫，仍被溢出击穿）：
#   守卫 alice >= amount（付款方余额充足），alice -= amount 不会为负；
#   但收款方 bob 初始已接近 INT_MAX，bob += amount 在 64 位补码下回绕为负数。
# send(100): alice'=0, bob'=(INT_MAX-50)+100=INT_MAX+50 -> -9223372036854775759。
# 成对加减在模 2^64 下恒守恒，因此这里只有非负性能抓到 —— 体现两种性质各自的作用。
OVERFLOW_TRANSFER = {
    "name": "overflow_transfer",
    "state_vars": [
        {"name": "alice", "type": "int", "init": 100},
        {"name": "bob", "type": "int", "init": INT_MAX - 50},
    ],
    "actions": [
        {
            "name": "send",
            "params": [{"name": "amount", "type": "int", "min": 0, "max": 1000}],
            "guards": [
                {"expr": {"t": "cmp", "op": ">=",
                          "lhs": {"t": "var", "name": "alice"},
                          "rhs": {"t": "var", "name": "amount"}}},
            ],
            "effects": [
                {"kind": "assign", "target": "alice",
                 "expr": {"t": "arith", "op": "-",
                          "args": [{"t": "var", "name": "alice"},
                                   {"t": "var", "name": "amount"}]}},
                {"kind": "assign", "target": "bob",
                 "expr": {"t": "arith", "op": "+",
                          "args": [{"t": "var", "name": "bob"},
                                   {"t": "var", "name": "amount"}]}},
            ],
        },
    ],
}

# 两步泄漏：leak 动作（只加 attacker、不扣 treasury）被 ready 锁挡住，
# 必须先 arm 置位，第 2 步才能泄漏 -> 最短反例恰为深度 2，用于步数边界测试。
LATE_LEAK = {
    "name": "late_leak",
    "state_vars": [
        {"name": "treasury", "type": "int", "init": 10},
        {"name": "attacker", "type": "int", "init": 0},
        {"name": "ready", "type": "bool", "init": False},
    ],
    "actions": [
        {
            "name": "arm",
            "params": [],
            "guards": [
                {"expr": {"t": "cmp", "op": "==",
                          "lhs": {"t": "var", "name": "ready"},
                          "rhs": {"t": "bool", "value": False}}},
            ],
            "effects": [{"kind": "lock", "target": "ready"}],
        },
        {
            "name": "idle",
            "params": [],
            "guards": [],
            "effects": [],
        },
        {
            "name": "leak",
            "params": [{"name": "amount", "type": "int", "min": 1, "max": 5}],
            "guards": [{"expr": {"t": "var", "name": "ready"}}],
            "effects": [
                {"kind": "assign", "target": "attacker",
                 "expr": {"t": "arith", "op": "+",
                          "args": [{"t": "var", "name": "attacker"},
                                   {"t": "var", "name": "amount"}]}},
            ],
        },
    ],
}

# 不可达状态：唯一可能破坏不变量的动作 steal 被矛盾守卫
# (vault>=100 且 vault<50) 永久锁住，inspect 是空转。任何有界深度都找不到
# 反例 —— 但检查器只报告“该步数内未发现”，不声称任意深度安全。
UNREACHABLE_GUARD = {
    "name": "unreachable_guard",
    "state_vars": [
        {"name": "vault", "type": "int", "init": 10},
        {"name": "hand", "type": "int", "init": 0},
    ],
    "actions": [
        {
            "name": "inspect",
            "params": [],
            "guards": [],
            "effects": [],  # 空转：状态不变
        },
        {
            "name": "steal",
            "params": [{"name": "amount", "type": "int", "min": 1, "max": 100}],
            "guards": [
                {"expr": {"t": "boolop", "op": "and", "args": [
                    {"t": "cmp", "op": ">=",
                     "lhs": {"t": "var", "name": "vault"},
                     "rhs": {"t": "num", "value": 100}},
                    {"t": "cmp", "op": "<",
                     "lhs": {"t": "var", "name": "vault"},
                     "rhs": {"t": "num", "value": 50}},
                ]}},
            ],
            "effects": [
                {"kind": "assign", "target": "vault",
                 "expr": {"t": "arith", "op": "-",
                          "args": [{"t": "var", "name": "vault"},
                                   {"t": "var", "name": "amount"}]}},
                {"kind": "assign", "target": "hand",
                 "expr": {"t": "arith", "op": "+",
                          "args": [{"t": "var", "name": "hand"},
                                   {"t": "var", "name": "amount"}]}},
            ],
        },
    ],
}

FIXTURES = {
    "safe_transfer": SAFE_TRANSFER,
    "missing_debit": MISSING_DEBIT,
    "overflow_transfer": OVERFLOW_TRANSFER,
    "late_leak": LATE_LEAK,
    "unreachable_guard": UNREACHABLE_GUARD,
}
