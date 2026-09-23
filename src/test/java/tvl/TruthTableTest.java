package tvl;

import java.util.Map;

import tvl.core.DataType;
import tvl.core.TriBool;
import tvl.core.Value;

/**
 * 验收点：穷举三值真值表。
 *
 * 真值集合 {TRUE, FALSE, UNKNOWN}，用数值编码 T=1, U=0, F=-1；
 * Kleene 逻辑的独立参照定义（不使用被测的 TriBool.and/or）：
 *   a AND b = min(a,b)，a OR b = max(a,b)，NOT a = -a。
 *
 * 每条取值组合都通过完整管线（解析 -> 类型检查 -> 求值器，
 * 且操作数来自真实布尔列而非字面量）验证，共：
 *   NOT 3 种 + AND 3x3=9 种 + OR 3x3=9 种 = 21 种，全部穷举。
 */
public class TruthTableTest {

    private static final TriBool[] TRIS = {TriBool.TRUE, TriBool.UNKNOWN, TriBool.FALSE};

    public static void run() {
        Map<String, DataType> schema = Harness.schema(
                "a", DataType.BOOLEAN, "b", DataType.BOOLEAN);
        String[] cols = {"a", "b"};

        // ---- NOT：3 种穷举 ----
        String[] notSrc = {"NOT a", "NOT (NOT a)"};
        for (TriBool a : TRIS) {
            Value[] row = Harness.rowV(triToValue(a));
            TriBool got = Harness.logic(notSrc[0],
                    Harness.schema("a", DataType.BOOLEAN),
                    new String[]{"a"}, row);
            TF.assertEquals(oracleNot(a), got);

            // 双重否定等于自身
            TriBool got2 = Harness.logic(notSrc[1],
                    Harness.schema("a", DataType.BOOLEAN),
                    new String[]{"a"}, row);
            TF.assertEquals(a, got2);
        }

        // ---- AND / OR：3x3 = 9 种穷举（各一遍） ----
        for (TriBool a : TRIS) {
            for (TriBool b : TRIS) {
                Value[] row = Harness.rowV(triToValue(a), triToValue(b));

                TriBool andGot = Harness.logic("a AND b", schema, cols, row);
                TF.assertEquals(oracleAnd(a, b), andGot);

                TriBool orGot = Harness.logic("a OR b", schema, cols, row);
                TF.assertEquals(oracleOr(a, b), orGot);

                // 德摩根定律：NOT(a AND b) == NOT a OR NOT b
                TriBool dm1 = Harness.logic("NOT (a AND b)", schema, cols, row);
                TriBool dm2 = Harness.logic("NOT a OR NOT b", schema, cols, row);
                TF.assertEquals(dm1, dm2);

                // 排中律/矛盾律在 3VL 下不成立的经典反例：
                // U OR U = U（不是 TRUE），U AND U = U（不是 FALSE）
                if (a == TriBool.UNKNOWN && b == TriBool.UNKNOWN) {
                    TF.assertEquals(TriBool.UNKNOWN, andGot);
                    TF.assertEquals(TriBool.UNKNOWN, orGot);
                }
            }
        }

        // ---- 与 SQL 规格逐条核对的关键单元格 ----
        TF.assertEquals(TriBool.UNKNOWN, TriBool.TRUE.and(TriBool.UNKNOWN));
        TF.assertEquals(TriBool.FALSE, TriBool.FALSE.and(TriBool.UNKNOWN));
        TF.assertEquals(TriBool.TRUE, TriBool.TRUE.or(TriBool.UNKNOWN));
        TF.assertEquals(TriBool.UNKNOWN, TriBool.FALSE.or(TriBool.UNKNOWN));
    }

    // ---- 独立 oracle：数值编码 + min/max/取负 ----

    private static int code(TriBool t) {
        return t == TriBool.TRUE ? 1 : t == TriBool.FALSE ? -1 : 0;
    }

    private static TriBool ofCode(int c) {
        return c > 0 ? TriBool.TRUE : c < 0 ? TriBool.FALSE : TriBool.UNKNOWN;
    }

    private static TriBool oracleAnd(TriBool a, TriBool b) {
        return ofCode(Math.min(code(a), code(b)));
    }

    private static TriBool oracleOr(TriBool a, TriBool b) {
        return ofCode(Math.max(code(a), code(b)));
    }

    private static TriBool oracleNot(TriBool a) {
        return ofCode(-code(a));
    }

    private static Value triToValue(TriBool t) {
        return t == TriBool.UNKNOWN ? Value.NULL
                : Value.ofBoolean(t == TriBool.TRUE);
    }
}
