package tvl;

import tvl.core.CompileException;
import tvl.core.DataType;

/**
 * 验收点：错误位置（行列/偏移）准确，类型检查拒绝非法表达式。
 *
 * 每个语法/语义错误都验证错误码与位置，确保错误能指回请求中的具体 token。
 */
public class ErrorPositionTest {

    public static void run() {
        unknownColumn();
        typeMismatch();
        syntaxErrors();
        positionsOnSameLine();
        positionsWithNewlines();
        invalidTokens();
        nonAssociativeComparison();
        danglingIs();
        unterminatedLiterals();
    }

    private static void unknownColumn() {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("a + 1",
                        Harness.schema("b", DataType.INTEGER)));
        TF.assertEquals("UNKNOWN_COLUMN", e.code());
        TF.assertEquals(0, e.pos().offset);
        TF.assertEquals(1, e.pos().line);
        TF.assertEquals(1, e.pos().column);
        TF.assertEquals(1, e.length()); // 列名 a 长度 1

        // 多字符列名、位于表达式中间
        CompileException e2 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 + price > 2",
                        Harness.schema("x", DataType.INTEGER)));
        TF.assertEquals("UNKNOWN_COLUMN", e2.code());
        TF.assertEquals(4, e2.pos().offset); // "1 + " 占 4 个字符
        TF.assertEquals(5, e2.length());     // price 长度 5
    }

    private static void typeMismatch() {
        var ints = Harness.schema("a", DataType.INTEGER, "b", DataType.INTEGER);

        // 字符串参与算术：错误定位到操作符 '+'
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("'x' + 1"));
        TF.assertEquals("TYPE_MISMATCH", e.code());
        // "'x' " 为 4 个字符，'+' 位于 offset 4
        TF.assertEquals(4, e.pos().offset);

        // 整数参与 AND
        CompileException e2 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 AND TRUE"));
        TF.assertEquals("TYPE_MISMATCH", e2.code());

        // 布尔参与算术
        CompileException e3 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("a + TRUE", ints));
        TF.assertEquals("TYPE_MISMATCH", e3.code());

        // 布尔不能排序比较，但可以 = / <>
        TF.assertThrows(CompileException.class,
                () -> Harness.compile("TRUE < FALSE"));
        Harness.compile("TRUE = FALSE");  // 合法
        Harness.compile("TRUE <> FALSE"); // 合法

        // 不同类型做相等比较
        TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 = 'a'"));
        TF.assertThrows(CompileException.class,
                () -> Harness.compile("'a' = 1"));

        // 但任一侧为 NULL 字面量允许（未定型）
        Harness.compile("a = NULL", ints);
        Harness.compile("NULL + a", ints);

        // NOT 作用于整数
        TF.assertThrows(CompileException.class,
                () -> Harness.compile("NOT 1", ints));
        Harness.compile("NOT TRUE");
        Harness.compile("NOT (a = 1)", ints);
    }

    private static void syntaxErrors() {
        // 空表达式
        TF.assertThrows(CompileException.class, () -> Harness.compile(""));
        TF.assertThrows(CompileException.class, () -> Harness.compile("   "));
        // 缺右操作数
        CompileException e1 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 +"));
        TF.assertEquals("SYNTAX_ERROR", e1.code());
        // 缺左操作数（AND 处）
        TF.assertThrows(CompileException.class, () -> Harness.compile("AND TRUE"));
        // 括号不匹配
        TF.assertThrows(CompileException.class, () -> Harness.compile("(1 + 2"));
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 + 2)"));
        // 双操作符
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 + * 2"));
        // 多余内容
        CompileException extra = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 2"));
        TF.assertEquals("SYNTAX_ERROR", extra.code());
    }

    private static void positionsOnSameLine() {
        // "a AND b +"：末尾缺操作数
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("a AND b +",
                        Harness.schema("a", DataType.BOOLEAN,
                                "b", DataType.INTEGER)));
        TF.assertEquals(9, e.pos().offset); // "a AND b +" 共 9 字符，EOF 在末尾
        TF.assertEquals(1, e.pos().line);

        // 错误指向操作符：a AND b（b 是整数）-> AND 操作符位置 offset 2
        CompileException e2 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("a AND b",
                        Harness.schema("a", DataType.BOOLEAN,
                                "b", DataType.INTEGER)));
        TF.assertEquals(2, e2.pos().offset);
        TF.assertEquals(3, e2.length()); // AND 三个字符
    }

    private static void positionsWithNewlines() {
        // 第二行上的未知列 bbb
        String src = "a = 1 AND\nbbb > 2";
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile(src,
                        Harness.schema("a", DataType.INTEGER)));
        TF.assertEquals("UNKNOWN_COLUMN", e.code());
        TF.assertEquals(2, e.pos().line);
        TF.assertEquals(1, e.pos().column);
        // 第一行长度 9（a = 1 AND）+ 换行 = offset 10
        TF.assertEquals(10, e.pos().offset);
    }

    private static void invalidTokens() {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 @ 2"));
        TF.assertEquals("UNEXPECTED_CHAR", e.code());
        TF.assertEquals(2, e.pos().offset);
    }

    private static void nonAssociativeComparison() {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 = 2 = 3"));
        TF.assertEquals("NON_ASSOCIATIVE_COMPARISON", e.code());
        // 指向第二个 '='
        TF.assertEquals(6, e.pos().offset);
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 < 2 > 3"));
        // 用括号可以显式组合
        Harness.compile("(1 = 2) = FALSE");
    }

    private static void danglingIs() {
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 IS"));
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 IS NOT"));
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 IS 2"));
        TF.assertEquals("SYNTAX_ERROR", e.code());
        TF.assertThrows(CompileException.class, () -> Harness.compile("1 IS NULL NULL"));
    }

    private static void unterminatedLiterals() {
        CompileException string = TF.assertThrows(CompileException.class,
                () -> Harness.compile("'abc"));
        TF.assertEquals("UNTERMINATED_STRING", string.code());
        TF.assertEquals(0, string.pos().offset);

        CompileException comment = TF.assertThrows(CompileException.class,
                () -> Harness.compile("1 /* nope"));
        TF.assertEquals("UNTERMINATED_COMMENT", comment.code());
    }
}
