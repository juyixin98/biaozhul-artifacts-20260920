package tvl;

import tvl.core.DataType;
import tvl.core.TriBool;
import tvl.core.Value;

/**
 * 验收点：运算符优先级、结合性与括号。
 *
 * 优先级（低 -> 高）：OR < AND < NOT < 比较 < + - < * / < 一元 - < 原子。
 * 这与 SQL 的惯例一致（比较紧于 NOT；NOT 紧于 AND；AND 紧于 OR）。
 */
public class PrecedenceTest {

    public static void run() {
        arithmetic();
        comparisonVsArithmetic();
        logicPrecedence();
        notPrecedence();
        isNullPrecedence();
        parentheses();
        unaryMinus();
        keywordsCaseInsensitive();
        comments();
    }

    private static void arithmetic() {
        // 乘除高于加减
        TF.assertEquals(14L, Harness.value("2 + 3 * 4").integer());
        TF.assertEquals(20L, Harness.value("(2 + 3) * 4").integer());
        TF.assertEquals(-5L, Harness.value("10 / 2 - 10").integer());
        // 左结合
        TF.assertEquals(5L, Harness.value("10 - 2 - 3").integer());
        TF.assertEquals(1L, Harness.value("20 / 4 / 5").integer());
        TF.assertEquals(14L, Harness.value("2 * 3 + 4 * 2").integer());
        TF.assertEquals(2L, Harness.value("7 / 3").integer());          // 整除向零
        TF.assertEquals(-2L, Harness.value("-7 / 3").integer());
        TF.assertEquals(-2L, Harness.value("7 / -3").integer());
    }

    private static void comparisonVsArithmetic() {
        TF.assertEquals(true, Harness.value("2 + 3 = 5").bool());
        TF.assertEquals(true, Harness.value("2 * 3 > 2 + 1").bool());
        TF.assertEquals(false, Harness.value("10 - 2 * 3 = 24").bool()); // 4 = 24 -> false
        TF.assertEquals(true, Harness.value("10 - 2 * 3 = 4").bool());
        // != 与 <> 等价
        TF.assertEquals(true, Harness.value("1 <> 2").bool());
        TF.assertEquals(true, Harness.value("1 != 2").bool());
        TF.assertEquals(false, Harness.value("1 <> 1").bool());
        TF.assertEquals(true, Harness.value("3 >= 3").bool());
        TF.assertEquals(false, Harness.value("3 > 3").bool());
        TF.assertEquals(true, Harness.value("'b' >= 'a'").bool());
        TF.assertEquals(true, Harness.value("'abc' < 'abd'").bool());
    }

    private static void logicPrecedence() {
        // AND 紧于 OR：TRUE OR FALSE AND FALSE => TRUE OR (FALSE AND FALSE) = TRUE
        TF.assertEquals(TriBool.TRUE, tri("TRUE OR FALSE AND FALSE"));
        // 若从左到右错误结合会是 (TRUE OR FALSE) AND FALSE = FALSE
        // FALSE OR TRUE AND TRUE => FALSE OR TRUE = TRUE
        TF.assertEquals(TriBool.TRUE, tri("FALSE OR TRUE AND TRUE"));
        // a AND b OR c AND d == (a AND b) OR (c AND d)
        TF.assertEquals(TriBool.FALSE, tri("FALSE AND TRUE OR FALSE AND TRUE"));
        TF.assertEquals(TriBool.TRUE, tri("TRUE AND FALSE OR TRUE AND TRUE"));
    }

    private static void notPrecedence() {
        // NOT 紧于 AND：NOT FALSE AND FALSE => (NOT FALSE) AND FALSE = TRUE AND FALSE = FALSE
        TF.assertEquals(TriBool.FALSE, tri("NOT FALSE AND FALSE"));
        TF.assertEquals(TriBool.TRUE, tri("NOT FALSE AND TRUE"));
        // 比较紧于 NOT（SQL 语义）：NOT 1 = 2 => NOT (1 = 2) = TRUE
        TF.assertEquals(TriBool.TRUE, tri("NOT 1 = 2"));
        TF.assertEquals(TriBool.FALSE, tri("NOT 1 = 1"));
        TF.assertEquals(TriBool.TRUE, tri("NOT NOT 1 = 1"));
    }

    private static void isNullPrecedence() {
        // IS NULL 与 AND：col IS NULL AND TRUE
        var schema = Harness.schema("x", DataType.INTEGER);
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("x IS NULL AND TRUE", schema,
                        new String[]{"x"}, Harness.rowV(Value.NULL)));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("x IS NULL AND TRUE", schema,
                        new String[]{"x"}, Harness.rowV(Value.ofInteger(1))));
        // NOT 作用于 IS NULL：NOT x IS NULL 等价 NOT (x IS NULL)
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("NOT x IS NULL", schema,
                        new String[]{"x"}, Harness.rowV(Value.NULL)));
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("NOT x IS NULL", schema,
                        new String[]{"x"}, Harness.rowV(Value.ofInteger(1))));
        // 显式 IS NOT NULL
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("x IS NOT NULL", schema,
                        new String[]{"x"}, Harness.rowV(Value.ofInteger(1))));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("x IS NOT NULL", schema,
                        new String[]{"x"}, Harness.rowV(Value.NULL)));
    }

    private static void parentheses() {
        TF.assertEquals(TriBool.FALSE, tri("(TRUE OR FALSE) AND FALSE"));
        TF.assertEquals(TriBool.TRUE, tri("NOT (FALSE AND TRUE)"));
        TF.assertEquals(10L, Harness.value("(2 + 3) * (6 - 4)").integer());
    }

    private static void unaryMinus() {
        TF.assertEquals(-6L, Harness.value("-2 * 3").integer());
        TF.assertEquals(6L, Harness.value("-2 * -3").integer());
        TF.assertEquals(-5L, Harness.value("-(2 + 3)").integer());
        TF.assertEquals(6L, Harness.value("3 - -3").integer());
    }

    private static void keywordsCaseInsensitive() {
        TF.assertEquals(TriBool.TRUE, tri("true or false"));
        TF.assertEquals(TriBool.FALSE, tri("True And False"));
        TF.assertEquals(TriBool.TRUE, tri("not false"));
        TF.assertEquals(TriBool.UNKNOWN, tri("NOT Null"));
    }

    private static void comments() {
        TF.assertEquals(3L, Harness.value("1 + -- line comment\n2").integer());
        TF.assertEquals(3L, Harness.value("1 /* block */ + 2").integer());
        TF.assertEquals(TriBool.TRUE,
                tri("TRUE -- keep going\n OR FALSE"));
    }

    private static TriBool tri(String src) {
        return Harness.value(src).isNull()
                ? TriBool.UNKNOWN
                : (Harness.value(src).bool() ? TriBool.TRUE : TriBool.FALSE);
    }
}
