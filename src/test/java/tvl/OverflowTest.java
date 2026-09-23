package tvl;

import java.util.Map;

import tvl.core.CompileException;
import tvl.core.DataType;
import tvl.core.EvalException;
import tvl.core.Value;

/**
 * 验收点：整数溢出与除零返回明确错误（错误码 INTEGER_OVERFLOW /
 * DIVISION_BY_ZERO），而不是静默回绕。
 *
 * 已知限制：源码中书写 -9223372036854775808 会在词法阶段以
 * INTEGER_OUT_OF_RANGE 拒绝（数字部分超出正数范围），这是有意的明确报错；
 * 该极端值可经列值、-(...) 等方式参与运算并正确检测溢出。
 */
public class OverflowTest {

    private static final long MAX = Long.MAX_VALUE;
    private static final long MIN = Long.MIN_VALUE;
    /** 用合法字面量在求值期构造出 MIN（源码不能直接书写 -MIN）。 */
    private static final String MIN_EXPR = "(" + (MIN + 1) + " - 1)";

    public static void run() {
        addSubOverflow();
        mulOverflow();
        divisionByZero();
        divisionOverflow();
        unaryNegateOverflow();
        literalOutOfRange();
        overflowViaColumns();
        normalBoundaryValuesOk();
    }

    private static void addSubOverflow() {
        expectOverflow(MAX + " + 1");
        expectOverflow(MIN_EXPR + " - 1");
        expectOverflow(MAX + " + " + MAX);
        expectOverflow(MIN_EXPR + " + " + MIN_EXPR);
        expectOverflow("0 - " + MIN_EXPR);
        // 边界不溢出
        TF.assertEquals(MIN + 1, Harness.value(MIN_EXPR + " + 1").integer());
        TF.assertEquals(MAX - 1, Harness.value(MAX + " - 1").integer());
        TF.assertEquals(MIN, Harness.value(MIN_EXPR + " - 0").integer());
    }

    private static void mulOverflow() {
        expectOverflow(MAX + " * 2");
        expectOverflow(MIN_EXPR + " * 2");
        expectOverflow(MAX + " * " + MAX);
        // MIN * -1 溢出
        expectOverflow(MIN_EXPR + " * -1");
        // 正常边界
        TF.assertEquals(0L, Harness.value(MAX + " * 0").integer());
        TF.assertEquals(MAX, Harness.value(MAX + " * 1").integer());
        TF.assertEquals(-MAX, Harness.value(MAX + " * -1").integer());
        // 32 位边界附近乘积
        TF.assertEquals(1000000L * 1000000L,
                Harness.value("1000000 * 1000000").integer());
    }

    private static void divisionByZero() {
        expectDiv0("1 / 0");
        expectDiv0("0 / 0");
        expectDiv0("-5 / 0");
        expectDiv0("(2 + 3) / (3 - 3)");
        // 整除余数被丢弃但不抛错
        TF.assertEquals(0L, Harness.value("1 / 2").integer());
    }

    private static void divisionOverflow() {
        // MIN / -1 = 9223372036854775808，超出 long，明确报溢出
        expectOverflow(MIN_EXPR + " / -1");
        // 其他负数除法正常
        TF.assertEquals(1L, Harness.value(MIN_EXPR + " / " + MIN_EXPR).integer());
        TF.assertEquals(MIN / 2, Harness.value(MIN_EXPR + " / 2").integer());
    }

    private static void unaryNegateOverflow() {
        // 一元负号作用于 MIN 溢出（用表达式 -(MIN+1-1) 让值落到 MIN）
        expectOverflow("-" + MIN_EXPR);
        // 普通一元负号正常
        TF.assertEquals(-5L, Harness.value("-5").integer());
    }

    private static void literalOutOfRange() {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile("99999999999999999999999"));
        TF.assertEquals("INTEGER_OUT_OF_RANGE", e.code());
        TF.assertNotNull(e.pos(), "溢出字面量错误应带位置");

        CompileException e2 = TF.assertThrows(CompileException.class,
                () -> Harness.compile("-9223372036854775808"));
        TF.assertEquals("INTEGER_OUT_OF_RANGE", e2.code());

        // 合法的边界字面量
        TF.assertEquals(MAX, Harness.value(MAX + "").integer());
        TF.assertEquals(MIN + 1, Harness.value((MIN + 1) + "").integer());
    }

    private static void overflowViaColumns() {
        Map<String, DataType> schema = Harness.schema(
                "a", DataType.INTEGER, "b", DataType.INTEGER);
        String[] cols = {"a", "b"};

        EvalException e = TF.assertThrows(EvalException.class, () ->
                Harness.value("a + b", schema, cols,
                        Harness.cells(MAX, 1L)));
        TF.assertEquals("INTEGER_OVERFLOW", e.code());

        EvalException d = TF.assertThrows(EvalException.class, () ->
                Harness.value("a / b", schema, cols,
                        Harness.cells(10L, 0L)));
        TF.assertEquals("DIVISION_BY_ZERO", d.code());

        // NULL 传播优先于运算：NULL/0 是 NULL，不抛除零（右侧仍会先求值，
        // 但 0 作为操作数本身不抛错，只有真正做除法时才检查除数）
        TF.assertTrue(Harness.value("a / b", schema, cols,
                Harness.cells(null, 0L)).isNull(), "NULL / 0 应为 NULL");
    }

    private static void normalBoundaryValuesOk() {
        TF.assertEquals(MAX + MIN,
                Harness.value(MAX + " + " + MIN_EXPR).integer()); // = -1
        TF.assertEquals(-1L, Harness.value(MAX + " + " + MIN_EXPR).integer());
        TF.assertEquals(MAX, Harness.value("4611686018427387903 * 2 + 1").integer());
    }

    private static void expectOverflow(String src) {
        EvalException e = TF.assertThrows(EvalException.class, () -> Harness.value(src));
        TF.assertEquals("INTEGER_OVERFLOW", e.code());
    }

    private static void expectDiv0(String src) {
        EvalException e = TF.assertThrows(EvalException.class, () -> Harness.value(src));
        TF.assertEquals("DIVISION_BY_ZERO", e.code());
    }
}
