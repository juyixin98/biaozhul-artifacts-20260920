package tvl;

import tvl.core.DataType;
import tvl.core.EvalException;
import tvl.core.TriBool;
import tvl.core.Value;
import java.util.Map;

/**
 * 验收点：短路求值。
 *
 *  - OR：左操作数为 TRUE 时不求右操作数；
 *  - AND：左操作数为 FALSE 时不求右操作数。
 *
 * 被跳过的分支里放置“一旦求值必然抛错”的表达式（除零、溢出），
 * 以此从行为上证明该分支确实没有执行；并设置对照组证明该分支
 * 未被跳过时错误会照常抛出。
 */
public class ShortCircuitTest {

    /** 右分支：除零，必然在求值时抛 DIVISION_BY_ZERO。 */
    private static final String DIV0_RHS = "1 / 0 = 1";
    /** 右分支：溢出，必然在求值时抛 INTEGER_OVERFLOW。 */
    private static final String OVF_RHS =
            "9223372036854775807 + 1 = 1";

    public static void run() {
        orSkipsOnTrue();
        andSkipsOnFalse();
        notSkippedControls();
        nullDoesNotShortCircuit();
        nestedShortCircuit();
        columnDrivenShortCircuit();
        errorsCarryCode();
    }

    private static void orSkipsOnTrue() {
        TF.assertEquals(TriBool.TRUE, tri("TRUE OR " + DIV0_RHS));
        TF.assertEquals(TriBool.TRUE, tri("TRUE OR " + OVF_RHS));
        // 左 TRUE 时右分支哪怕语法上是一整棵复杂表达式也不求值
        TF.assertEquals(TriBool.TRUE,
                tri("TRUE OR (1 / 0 > 2 AND FALSE OR NOT TRUE)"));
        // 右 TRUE 不能救左 FALSE 之外的错误：左 TRUE 时整体短路，无关于右侧
        TF.assertEquals(TriBool.TRUE,
                tri("(1 = 1) OR (1 / 0 = 0)"));
    }

    private static void andSkipsOnFalse() {
        TF.assertEquals(TriBool.FALSE, tri("FALSE AND " + DIV0_RHS));
        TF.assertEquals(TriBool.FALSE, tri("FALSE AND " + OVF_RHS));
        TF.assertEquals(TriBool.FALSE,
                tri("(1 = 2) AND (1 / 0 = 1)"));
        TF.assertEquals(TriBool.FALSE,
                tri("1 > 100 AND (9223372036854775807 + 1 = 0)"));
    }

    /** 对照组：不满足短路条件时，右分支被求值，错误照常抛出。 */
    private static void notSkippedControls() {
        EvalException e1 = TF.assertThrows(EvalException.class,
                () -> Harness.value("FALSE OR " + DIV0_RHS));
        TF.assertEquals("DIVISION_BY_ZERO", e1.code());

        EvalException e2 = TF.assertThrows(EvalException.class,
                () -> Harness.value("TRUE AND " + DIV0_RHS));
        TF.assertEquals("DIVISION_BY_ZERO", e2.code());

        TF.assertThrows(EvalException.class,
                () -> Harness.value("FALSE OR " + OVF_RHS));
        TF.assertThrows(EvalException.class,
                () -> Harness.value("TRUE AND " + OVF_RHS));
    }

    /**
     * UNKNOWN 不产生短路（Kleene 逻辑必须查看另一侧）：
     *  - U AND <除零>：右分支仍要求值 -> 抛错
     *  - U OR  <除零>：右分支仍要求值 -> 抛错
     */
    private static void nullDoesNotShortCircuit() {
        TF.assertThrows(EvalException.class,
                () -> Harness.value("(1 = NULL) AND " + DIV0_RHS));
        TF.assertThrows(EvalException.class,
                () -> Harness.value("(1 = NULL) OR " + DIV0_RHS));
        // 但 U OR TRUE 在右侧为字面量 TRUE 时安全返回 TRUE
        TF.assertEquals(TriBool.TRUE, tri("(1 = NULL) OR TRUE"));
        TF.assertEquals(TriBool.FALSE, tri("(1 = NULL) AND FALSE"));
    }

    private static void nestedShortCircuit() {
        // 内层 AND 被 FALSE 短路，整个 OR 左真又短路：两处除零都不触发
        TF.assertEquals(TriBool.TRUE,
                tri("TRUE OR (FALSE AND (1 / 0 = 1))"));
        // (FALSE AND <除零>) = FALSE；外层 OR 结果 FALSE
        TF.assertEquals(TriBool.FALSE,
                tri("FALSE OR (FALSE AND (1 / 0 = 1))"));
        // NOT 不改变短路结构
        TF.assertEquals(TriBool.FALSE,
                tri("NOT (TRUE OR (1 / 0 = 1))"));
        TF.assertEquals(TriBool.TRUE,
                tri("NOT (FALSE AND (1 / 0 = 1))"));
    }

    /** 用真实列值驱动短路（端到端，而非仅字面量）。 */
    private static void columnDrivenShortCircuit() {
        Map<String, DataType> schema = Harness.schema(
                "flag", DataType.BOOLEAN,
                "den", DataType.INTEGER);
        String[] cols = {"flag", "den"};

        // flag=TRUE 时 OR 短路：den=0 的行也不应触发除零
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("flag OR (10 / den = 1)", schema, cols,
                        Harness.cells(Boolean.TRUE, 0L)));
        // flag=FALSE 时 AND 短路
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("flag AND (10 / den = 1)", schema, cols,
                        Harness.cells(Boolean.FALSE, 0L)));
        // 对照组：flag=FALSE 的 OR 不短路，den=0 -> 除零
        TF.assertThrows(EvalException.class, () -> Harness.logic(
                "flag OR (10 / den = 1)", schema, cols,
                Harness.cells(Boolean.FALSE, 0L)));
        // 对照组：flag=TRUE 的 AND 不短路，den=0 -> 除零
        TF.assertThrows(EvalException.class, () -> Harness.logic(
                "flag AND (10 / den = 1)", schema, cols,
                Harness.cells(Boolean.TRUE, 0L)));
        // 正常行（den 非零）结果正确
        TF.assertEquals(TriBool.TRUE,
                Harness.logic("flag OR (10 / den = 1)", schema, cols,
                        Harness.cells(Boolean.FALSE, 10L)));
        TF.assertEquals(TriBool.FALSE,
                Harness.logic("flag OR (10 / den = 1)", schema, cols,
                        Harness.cells(Boolean.FALSE, 3L)));
    }

    private static void errorsCarryCode() {
        EvalException overflow = TF.assertThrows(EvalException.class,
                () -> Harness.value("1 / 0"));
        TF.assertEquals("DIVISION_BY_ZERO", overflow.code());
        EvalException mulOvf = TF.assertThrows(EvalException.class,
                () -> Harness.value(
                        "9223372036854775807 * 9223372036854775807"));
        TF.assertEquals("INTEGER_OVERFLOW", mulOvf.code());
    }

    private static TriBool tri(String src) {
        Value v = Harness.value(src);
        return v.isNull() ? TriBool.UNKNOWN
                : (v.bool() ? TriBool.TRUE : TriBool.FALSE);
    }
}
