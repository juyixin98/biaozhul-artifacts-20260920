package com.tvl.test;

import com.tvl.core.Ternary;

/**
 * 三值逻辑真值表穷举：3×3 枚举 AND/OR 输入组合，NOT 枚举 3 种输入。
 * 期望值按 SQL:1999 标准手工列出，验证枚举实现与位图字节实现一致。
 */
final class TernaryTruthTableTest {

    private TernaryTruthTableTest() {
    }

    static void register(TestRunner runner) {
        runner.add("3VL 真值表: AND 3x3 穷举", a -> {
            // 行=左操作数，列=右操作数；顺序 FALSE, UNKNOWN, TRUE
            Ternary F = Ternary.FALSE, U = Ternary.UNKNOWN, T = Ternary.TRUE;
            Ternary[][] expectedAnd = {
                    {F, F, F},
                    {F, U, U},
                    {F, U, T}
            };
            Ternary[] vals = {F, U, T};
            for (int i = 0; i < 3; i++) {
                for (int j = 0; j < 3; j++) {
                    a.eq(vals[i].and(vals[j]), expectedAnd[i][j],
                            vals[i] + " AND " + vals[j]);
                    // 字节级（向量路径）一致
                    a.eq(Ternary.ofCode(Ternary.andCode(vals[i].code, vals[j].code)),
                            expectedAnd[i][j],
                            "字节 AND(" + vals[i] + "," + vals[j] + ")");
                }
            }
        });

        runner.add("3VL 真值表: OR 3x3 穷举", a -> {
            Ternary F = Ternary.FALSE, U = Ternary.UNKNOWN, T = Ternary.TRUE;
            Ternary[][] expectedOr = {
                    {F, U, T},
                    {U, U, T},
                    {T, T, T}
            };
            Ternary[] vals = {F, U, T};
            for (int i = 0; i < 3; i++) {
                for (int j = 0; j < 3; j++) {
                    a.eq(vals[i].or(vals[j]), expectedOr[i][j],
                            vals[i] + " OR " + vals[j]);
                    a.eq(Ternary.ofCode(Ternary.orCode(vals[i].code, vals[j].code)),
                            expectedOr[i][j],
                            "字节 OR(" + vals[i] + "," + vals[j] + ")");
                }
            }
        });

        runner.add("3VL 真值表: NOT 与 WHERE 选择语义", a -> {
            a.eq(Ternary.TRUE.not(), Ternary.FALSE, "NOT TRUE");
            a.eq(Ternary.FALSE.not(), Ternary.TRUE, "NOT FALSE");
            a.eq(Ternary.UNKNOWN.not(), Ternary.UNKNOWN, "NOT UNKNOWN 必须仍是 UNKNOWN");
            a.eq(Ternary.ofCode(Ternary.notCode(Ternary.UNKNOWN.code)),
                    Ternary.UNKNOWN, "字节 NOT UNKNOWN");

            // WHERE 只放行 TRUE
            a.check(Ternary.TRUE.isSelected(), "TRUE 必须被 WHERE 放行");
            a.check(!Ternary.FALSE.isSelected(), "FALSE 必须被 WHERE 过滤");
            a.check(!Ternary.UNKNOWN.isSelected(), "UNKNOWN 必须和 FALSE 一样被过滤");

            // 德摩根在 3VL 下成立：NOT(a AND b) == NOT a OR NOT b
            Ternary[] vals = Ternary.values();
            for (Ternary x : vals) {
                for (Ternary y : vals) {
                    a.eq(x.and(y).not(), x.not().or(y.not()),
                            "德摩根 NOT(" + x + " AND " + y + ")");
                }
            }
        });
    }
}
