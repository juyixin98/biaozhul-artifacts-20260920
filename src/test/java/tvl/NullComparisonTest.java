package tvl;

import java.util.Map;

import tvl.core.DataType;
import tvl.core.TriBool;
import tvl.core.Value;

/**
 * 验收点：NULL 参与比较的 SQL 三值语义。
 *
 * 穷举：左操作数 3 种取值（含 NULL）x 右操作数 3 种取值 x 6 种比较符，
 * 对 INTEGER 与 STRING 各一遍，并覆盖“字面量 NULL”与“列值 NULL”两种来源。
 *
 * SQL 规则：只要一侧为 NULL，= &lt;&gt; &lt; &lt;= &gt; &gt;= 结果一律 UNKNOWN。
 * 特别注意 NULL &lt;&gt; NULL 也是 UNKNOWN（不是 TRUE），必须用 IS NULL 判空。
 */
public class NullComparisonTest {

    public static void run() {
        integerExhaustive();
        stringExhaustive();
        literalNullCases();
        nullPropagation();
        isNullCases();
        filterSemantics();
    }

    private static void integerExhaustive() {
        Value[] vals = {Value.ofInteger(1), Value.NULL, Value.ofInteger(2)};
        String[][] ops = {
                {"=", "eq"}, {"<>", "ne"}, {"<", "lt"},
                {"<=", "le"}, {">", "gt"}, {">=", "ge"}
        };
        Map<String, DataType> schema = Harness.schema(
                "a", DataType.INTEGER, "b", DataType.INTEGER);

        for (Value a : vals) {
            for (Value b : vals) {
                for (String[] op : ops) {
                    TriBool expected = oracleInt(op[0], a, b);
                    String src = "a " + op[0] + " b";
                    TriBool got = Harness.logic(src, schema,
                            new String[]{"a", "b"}, Harness.rowV(a, b));
                    TF.assertEquals(expected, got);
                }
            }
        }
    }

    private static TriBool oracleInt(String op, Value a, Value b) {
        if (a.isNull() || b.isNull()) {
            return TriBool.UNKNOWN;
        }
        long x = a.integer();
        long y = b.integer();
        int c = Long.compare(x, y);
        switch (op) {
            case "=":  return c == 0 ? TriBool.TRUE : TriBool.FALSE;
            case "<>": return c != 0 ? TriBool.TRUE : TriBool.FALSE;
            case "<":  return c < 0 ? TriBool.TRUE : TriBool.FALSE;
            case "<=": return c <= 0 ? TriBool.TRUE : TriBool.FALSE;
            case ">":  return c > 0 ? TriBool.TRUE : TriBool.FALSE;
            default:   return c >= 0 ? TriBool.TRUE : TriBool.FALSE;
        }
    }

    private static void stringExhaustive() {
        Value[] vals = {Value.ofString("a"), Value.NULL, Value.ofString("b")};
        String[] ops = {"=", "<>", "<", "<=", ">", ">="};
        Map<String, DataType> schema = Harness.schema(
                "a", DataType.STRING, "b", DataType.STRING);

        for (Value a : vals) {
            for (Value b : vals) {
                for (String op : ops) {
                    TriBool expected;
                    if (a.isNull() || b.isNull()) {
                        expected = TriBool.UNKNOWN;
                    } else {
                        int c = a.string().compareTo(b.string());
                        boolean r;
                        switch (op) {
                            case "=":  r = c == 0; break;
                            case "<>": r = c != 0; break;
                            case "<":  r = c < 0;  break;
                            case "<=": r = c <= 0; break;
                            case ">":  r = c > 0;  break;
                            default:   r = c >= 0; break;
                        }
                        expected = r ? TriBool.TRUE : TriBool.FALSE;
                    }
                    TriBool got = Harness.logic("a " + op + " b", schema,
                            new String[]{"a", "b"}, Harness.rowV(a, b));
                    TF.assertEquals(expected, got);
                }
            }
        }
    }

    /** 字面量 NULL：NULL = NULL 也是 UNKNOWN。 */
    private static void literalNullCases() {
        TF.assertEquals(TriBool.UNKNOWN, toTri(Harness.value("NULL = NULL")));
        TF.assertEquals(TriBool.UNKNOWN, toTri(Harness.value("NULL <> NULL")));
        TF.assertEquals(TriBool.UNKNOWN, toTri(Harness.value("1 = NULL")));
        TF.assertEquals(TriBool.UNKNOWN, toTri(Harness.value("NULL = 1")));
        TF.assertEquals(TriBool.UNKNOWN, toTri(Harness.value("'x' < NULL")));
        TF.assertTrue(Harness.value("NULL IS NULL").bool(), "NULL IS NULL 应为 TRUE");
        TF.assertFalse(Harness.value("NULL IS NOT NULL").bool(),
                "NULL IS NOT NULL 应为 FALSE");
        TF.assertTrue(Harness.value("1 IS NOT NULL").bool(), "1 IS NOT NULL 为 TRUE");
        TF.assertFalse(Harness.value("1 IS NULL").bool(), "1 IS NULL 为 FALSE");
    }

    /** 算术与 AND/OR 中的 NULL 传播。 */
    private static void nullPropagation() {
        Map<String, DataType> schema = Harness.schema("a", DataType.INTEGER);
        String[] cols = {"a"};
        Value n = Value.NULL;

        TF.assertTrue(Harness.value("a + 1", schema, cols, Harness.rowV(n)).isNull(),
                "NULL + 1 为 NULL");
        TF.assertTrue(Harness.value("a * 5", schema, cols, Harness.rowV(n)).isNull(),
                "NULL * 5 为 NULL");

        // U AND T = U, U AND F = F, U OR T = T, U OR F = U
        TF.assertEquals(TriBool.UNKNOWN,
                Harness.logic("(a = 1) AND TRUE", schema, cols, Harness.rowV(n)));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("(a = 1) AND FALSE", schema, cols, Harness.rowV(n)));
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("(a = 1) OR TRUE", schema, cols, Harness.rowV(n)));
        TF.assertEquals(TriBool.UNKNOWN,
                Harness.logic("(a = 1) OR FALSE", schema, cols, Harness.rowV(n)));
        TF.assertEquals(TriBool.UNKNOWN,
                Harness.logic("NOT (a = 1)", schema, cols, Harness.rowV(n)));
    }

    private static void isNullCases() {
        Map<String, DataType> schema = Harness.schema("a", DataType.INTEGER);
        String[] cols = {"a"};
        // IS NULL 精确判空，不受三值影响
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("a IS NULL", schema, cols, Harness.rowV(Value.NULL)));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("a IS NULL", schema, cols, Harness.rowV(Value.ofInteger(0))));
        // 组合：a IS NULL OR a > 0
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("a IS NULL OR a > 0", schema, cols,
                        Harness.rowV(Value.NULL)));
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("a IS NULL OR a > 0", schema, cols,
                        Harness.rowV(Value.ofInteger(5))));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("a IS NULL OR a > 0", schema, cols,
                        Harness.rowV(Value.ofInteger(0))));
    }

    /** WHERE 过滤：仅 TRUE 选中；UNKNOWN 与 FALSE 一样被排除。 */
    private static void filterSemantics() {
        Map<String, DataType> schema = Harness.schema("a", DataType.INTEGER);
        String[] cols = {"a"};
        // 1 = 1 -> TRUE（选中）；NULL 行 -> UNKNOWN（排除）；0 行 0=1 FALSE（排除）
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("a = a", schema, cols, Harness.rowV(Value.ofInteger(1))));
        TF.assertEquals(TriBool.UNKNOWN,
                Harness.logic("a = a", schema, cols, Harness.rowV(Value.NULL)));
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("a = a", schema, cols, Harness.rowV(Value.ofInteger(0))));
    }

    private static TriBool toTri(Value v) {
        if (v.isNull()) {
            return TriBool.UNKNOWN;
        }
        return v.bool() ? TriBool.TRUE : TriBool.FALSE;
    }
}
