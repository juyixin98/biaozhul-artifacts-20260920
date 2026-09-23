package tvl;

import tvl.core.CompileException;
import tvl.core.DataType;
import tvl.parser.Expr;

/** 类型检查器：合法形式通过并回填结果类型，非法形式拒绝。 */
public class TypeCheckTest {

    private static final DataType I = DataType.INTEGER;
    private static final DataType B = DataType.BOOLEAN;
    private static final DataType S = DataType.STRING;
    private static final DataType N = DataType.NULL;

    public static void run() {
        validForms();
        resultTypes();
        nullLiteralsAreFlexible();
        invalidForms();
    }

    private static void validForms() {
        Harness.compile("a + b * 2", Harness.schema("a", I, "b", I));
        Harness.compile("a = 1 AND b <> 2", Harness.schema("a", I, "b", I));
        Harness.compile("s1 = s2 OR s1 < 'z'", Harness.schema("s1", S, "s2", S));
        Harness.compile("a IS NULL AND b IS NOT NULL",
                Harness.schema("a", I, "b", B));
        Harness.compile("NOT (c OR FALSE)", Harness.schema("c", B));
        Harness.compile("-a / 2", Harness.schema("a", I));
        // IS NULL 可作用于任意类型（包括布尔列、字符串列）
        Harness.compile("c IS NULL", Harness.schema("c", B));
        Harness.compile("s IS NULL", Harness.schema("s", S));
    }

    private static void resultTypes() {
        Expr arith = Harness.compile("1 + 2 * 3");
        TF.assertEquals(I, arith.resolvedType);
        Expr cmp = Harness.compile("'a' < 'b'");
        TF.assertEquals(B, cmp.resolvedType);
        Expr log = Harness.compile("TRUE AND NOT FALSE");
        TF.assertEquals(B, log.resolvedType);
        Expr isn = Harness.compile("1 IS NULL");
        TF.assertEquals(B, isn.resolvedType);
        Expr nullLit = Harness.compile("NULL");
        TF.assertEquals(N, nullLit.resolvedType);
        // 列类型回填
        Expr col = Harness.compile("a", Harness.schema("a", S));
        TF.assertEquals(S, col.resolvedType);
        // NULL 参与算术 -> 结果 INTEGER
        Expr nArith = Harness.compile("NULL + a", Harness.schema("a", I));
        TF.assertEquals(I, nArith.resolvedType);
        // NULL 参与相等比较 -> BOOLEAN
        Expr nCmp = Harness.compile("a = NULL", Harness.schema("a", S));
        TF.assertEquals(B, nCmp.resolvedType);
    }

    private static void nullLiteralsAreFlexible() {
        Harness.compile("-NULL");
        Harness.compile("NULL AND NULL");
        Harness.compile("NULL OR NOT NULL");
        Harness.compile("NULL = NULL");
        Harness.compile("'x' <> NULL");
        Harness.compile("NULL < 5");
        Harness.compile("(a + 1) IS NULL", Harness.schema("a", I));
    }

    private static void invalidForms() {
        assertType("'s' - 1");
        assertType("1 + TRUE");
        assertType("TRUE * FALSE");
        assertType("a OR b", Harness.schema("a", I, "b", I));
        assertType("NOT a", Harness.schema("a", I));
        assertType("a < b", Harness.schema("a", B, "b", B));
        assertType("a >= b", Harness.schema("a", B, "b", I));
        assertType("a = b", Harness.schema("a", I, "b", S));
        assertType("a + b", Harness.schema("a", I, "b", S));
        // 空模式下引用列 -> UNKNOWN_COLUMN（单列错误码）
        CompileException noCol = TF.assertThrows(CompileException.class,
                () -> Harness.compile("a", Harness.schema("b", I)));
        TF.assertEquals("UNKNOWN_COLUMN", noCol.code());
        // NULL 不能直接用排序比较混合两具体不同类型仍按非 NULL 侧规则：
        // 这里两侧都具体且类型不同 -> 报错
        assertType("1 < 'a'");
    }

    private static void assertType(String src) {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile(src));
        TF.assertEquals("TYPE_MISMATCH", e.code());
    }

    private static void assertType(String src, java.util.Map<String, DataType> sch) {
        CompileException e = TF.assertThrows(CompileException.class,
                () -> Harness.compile(src, sch));
        TF.assertEquals("TYPE_MISMATCH", e.code());
    }
}
