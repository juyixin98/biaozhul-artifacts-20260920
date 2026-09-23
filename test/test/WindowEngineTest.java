package test;

import engine.model.ColumnType;
import engine.model.Relation;
import engine.model.Relation.Row;
import engine.window.BoundKind;
import engine.window.CheckedLong;
import engine.window.Frame;
import engine.window.FrameBound;
import engine.window.FunctionSpec;
import engine.window.NullOrder;
import engine.window.OrderKey;
import engine.window.WindowException;
import engine.window.WindowEngine;
import engine.window.WindowSpec;
import testutil.TestHarness;

import java.util.ArrayList;
import java.util.List;

/** 窗口引擎功能测试：ROW_NUMBER / RANK / 滑动 ROWS 求和 / NULL / 越界 / 负值 / 溢出。 */
public final class WindowEngineTest {

    public static boolean main(String[] args) {
        TestHarness t = new TestHarness("窗口引擎功能");

        rowNumberTests(t);
        rankTests(t);
        tieAndNullTests(t);
        slidingFrameTests(t);
        negativeTests(t);
        overflowTests(t);
        validationTests(t);
        checkedLongTests(t);

        return t.report();
    }

    // ------------------------------------------------------------------

    /** 构造测试关系：列规格 "name:TYPE"，行值用 null/Long/String。 */
    static Relation relation(String[] cols, Object[]... data) {
        List<String> names = new ArrayList<>();
        List<ColumnType> types = new ArrayList<>();
        for (String c : cols) {
            String[] parts = c.split(":");
            names.add(parts[0]);
            types.add(parts[1].equals("LONG") ? ColumnType.LONG : ColumnType.STRING);
        }
        List<Row> rows = new ArrayList<>();
        for (int i = 0; i < data.length; i++) {
            rows.add(new Row(i, data[i]));
        }
        return new Relation(names, types, rows);
    }

    private static List<OrderKey> orders(Object... kv) {
        List<OrderKey> keys = new ArrayList<>();
        for (int i = 0; i < kv.length; i += 3) {
            keys.add(new OrderKey((String) kv[i], (Boolean) kv[i + 1], (NullOrder) kv[i + 2]));
        }
        return keys;
    }

    private static Object[] col(Relation r, String name) {
        int idx = r.columnIndex(name);
        return r.rows().stream().map(row -> row.get(idx)).toArray();
    }

    // ------------------------------------------------------------------

    private static void rowNumberTests(TestHarness t) {
        // 分区 + 排序，并列键按输入次序决胜
        Relation in = relation(new String[]{"p:STRING", "v:LONG"},
                new Object[]{"a", 10L}, new Object[]{"b", 1L},
                new Object[]{"a", 10L}, new Object[]{"a", 5L});
        WindowSpec spec = new WindowSpec(List.of("p"),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.rowNumber("rn")));
        Relation out = new WindowEngine().execute(in, spec);

        t.eq(new String[]{"p", "v", "rn"}, out.columnNames().toArray(), "结果列在末尾追加");
        t.eq(List.of(ColumnType.STRING, ColumnType.LONG, ColumnType.LONG),
                out.columnTypes(), "结果列为 LONG");

        Object[] p = col(out, "p");
        Object[] rn = col(out, "rn");
        t.eq(4, out.rows().size(), "行数不变");
        t.eq("a", p[0], "分区 a 在前（字符串排序）");
        t.eq("a", p[1], "分区 a 连续");
        t.eq("a", p[2], "分区 a 连续");
        t.eq("b", p[3], "分区 b 在后");
        t.eq(1L, rn[0], "a 组第 1 行（v=5）");
        // 两个 v=10 并列，源行 0 在源行 2 之前
        t.eq(2L, rn[1], "a 组并列键 v=10 第 2 行取较早源行");
        t.eq(3L, rn[2], "a 组并列键 v=10 第 3 行取较晚源行");
        t.eq(1L, rn[3], "b 组重新从 1 开始");
        t.eq(5L, out.rows().get(0).get(1), "a 组首行 v=5");
        t.eq(10L, out.rows().get(1).get(1), "a 组第 2 行 v=10（源行 0）");
        t.eq(0L, out.rows().get(1).sourceIndex, "并列决胜依据源下标 0");
        t.eq(2L, out.rows().get(2).sourceIndex, "并列决胜依据源下标 2");
    }

    private static void rankTests(TestHarness t) {
        // 经典并列排名：10,10,10,20,20,30 -> RANK 1,1,1,4,4,6
        Relation in = relation(new String[]{"v:LONG"},
                new Object[]{30L}, new Object[]{10L}, new Object[]{20L},
                new Object[]{10L}, new Object[]{10L}, new Object[]{20L});
        WindowSpec spec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.rank("rk")));
        Relation out = new WindowEngine().execute(in, spec);
        t.eq(new Object[]{1L, 1L, 1L, 4L, 4L, 6L}, col(out, "rk"),
                "RANK 并列且有跳跃（1,1,1,4,4,6）");

        // 无排序键：全部并列第 1
        Relation in2 = relation(new String[]{"v:LONG"},
                new Object[]{9L}, new Object[]{1L});
        WindowSpec spec2 = new WindowSpec(List.of(), List.of(),
                List.of(FunctionSpec.rank("rk")));
        Relation out2 = new WindowEngine().execute(in2, spec2);
        t.eq(new Object[]{1L, 1L}, col(out2, "rk"), "无 ORDER BY 时 RANK 恒为 1");
    }

    private static void tieAndNullTests(TestHarness t) {
        // ASC：默认 NULLS LAST
        Relation in = relation(new String[]{"v:LONG"},
                new Object[]{2L}, new Object[]{null}, new Object[]{1L},
                new Object[]{null}, new Object[]{2L});
        WindowSpec spec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.rowNumber("rn"), FunctionSpec.rank("rk")));
        Relation out = new WindowEngine().execute(in, spec);
        Object[] v = col(out, "v");
        t.eq(1L, v[0], "ASC NULLS LAST：非空 1 最先");
        t.eq(2L, v[1], "并列 2 其一");
        t.eq(2L, v[2], "并列 2 其二");
        t.eq(null, v[3], "NULL 在末尾其一");
        t.eq(null, v[4], "NULL 在末尾其二");
        t.eq(new Object[]{1L, 2L, 2L, 4L, 4L}, col(out, "rk"),
                "两个 NULL 互相并列；RANK 跳过名次");
        t.eq(new Object[]{1L, 2L, 3L, 4L, 5L}, col(out, "rn"),
                "ROW_NUMBER 永不重复");

        // DESC：默认 NULLS FIRST（SQL/PostgreSQL）
        WindowSpec descSpec = new WindowSpec(List.of(),
                orders("v", false, NullOrder.NULLS_FIRST),
                List.of(FunctionSpec.rank("rk")));
        Relation desc = new WindowEngine().execute(in, descSpec);
        Object[] dv = col(desc, "v");
        t.eq(null, dv[0], "DESC NULLS FIRST：NULL 最先");
        t.eq(2L, dv[2], "DESC：2 在 1 前");
        t.eq(1L, dv[4], "DESC：1 最后");

        // 显式 ASC + NULLS FIRST
        WindowSpec explicit = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_FIRST),
                List.of(FunctionSpec.rank("rk")));
        Relation ex = new WindowEngine().execute(in, explicit);
        t.eq(null, col(ex, "v")[0], "显式 NULLS FIRST 覆盖 ASC 默认");
        t.eq(1L, col(ex, "rk")[0], "NULL 组排第 1");
        t.eq(3L, col(ex, "rk")[2], "NULL 占两名次后，1 排第 3");

        // 多列排序键上的并列
        Relation in2 = relation(new String[]{"a:STRING", "b:LONG"},
                new Object[]{"x", 1L}, new Object[]{"x", 1L},
                new Object[]{"x", 2L}, new Object[]{"y", 1L});
        WindowSpec spec2 = new WindowSpec(List.of(),
                orders("a", true, NullOrder.NULLS_LAST,
                        "b", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.rank("rk")));
        Relation out2 = new WindowEngine().execute(in2, spec2);
        t.eq(new Object[]{1L, 1L, 3L, 4L}, col(out2, "rk"),
                "多列排序键：(x,1) 并列，(x,2) 第 3，(y,1) 第 4");
    }

    private static void slidingFrameTests(TestHarness t) {
        // 帧 ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING（居中三行）
        Relation in = relation(new String[]{"v:LONG"},
                new Object[]{1L}, new Object[]{2L}, new Object[]{3L},
                new Object[]{4L}, new Object[]{5L});
        Frame frame3 = new Frame(
                new FrameBound(BoundKind.PRECEDING, 1),
                new FrameBound(BoundKind.FOLLOWING, 1));
        WindowSpec spec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", frame3)));
        Relation out = new WindowEngine().execute(in, spec);
        // 越界截断：1+2=3，1+2+3=6，2+3+4=9，3+4+5=12，4+5=9
        t.eq(new Object[]{3L, 6L, 9L, 12L, 9L}, col(out, "s"),
                "居中三行滑动求和，两端越界截断");

        // 默认帧（UNBOUNDED PRECEDING .. CURRENT ROW）= 累计和
        WindowSpec cumSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", Frame.defaultForSum())));
        Relation cum = new WindowEngine().execute(in, cumSpec);
        t.eq(new Object[]{1L, 3L, 6L, 10L, 15L}, col(cum, "s"),
                "默认帧为累计和");

        // 前向帧 1 FOLLOWING .. 2 FOLLOWING：最后两行窗口为空 -> NULL
        Frame lead = new Frame(
                new FrameBound(BoundKind.FOLLOWING, 1),
                new FrameBound(BoundKind.FOLLOWING, 2));
        WindowSpec leadSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", lead)));
        Relation leadOut = new WindowEngine().execute(in, leadSpec);
        // i=3 时帧起点 = 行4、终点越界截断到行4：帧内仅行4，值=5（不是空帧）
        // i=4 时帧起点=行5 已在分区外：空帧 -> NULL
        t.eq(new Object[]{5L, 7L, 9L, 5L, null}, col(leadOut, "s"),
                "前向窗口越界截断；整体移出分区后为空帧 -> NULL");

        // 超大偏移（接近 Long.MAX_VALUE）：饱和加减，不得因回绕产生错误帧
        Frame hugeLead = new Frame(
                new FrameBound(BoundKind.FOLLOWING, Long.MAX_VALUE - 1),
                FrameBound.unboundedFollowing());
        WindowSpec hugeSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", hugeLead)));
        Relation hugeOut = new WindowEngine().execute(in, hugeSpec);
        t.eq(new Object[]{null, null, null, null, null}, col(hugeOut, "s"),
                "超大前向偏移：所有行的帧都在分区外 -> NULL（无 long 回绕）");

        // 终点为“1 PRECEDING”（帧 UNBOUNDED PRECEDING .. 当前行前一行）：
        // 首行没有前一行 -> 空帧；其余行为不含当前行的累计和
        Frame backOne = new Frame(
                FrameBound.unboundedPreceding(),
                new FrameBound(BoundKind.PRECEDING, 1));
        WindowSpec backOneSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", backOne)));
        Relation backOneOut = new WindowEngine().execute(in, backOneSpec);
        t.eq(new Object[]{null, 1L, 3L, 6L, 10L}, col(backOneOut, "s"),
                "帧终点为 1 PRECEDING：首行空帧，其余为不含当前行的累计和");

        Frame hugeBack = new Frame(
                FrameBound.unboundedPreceding(),
                new FrameBound(BoundKind.PRECEDING, Long.MAX_VALUE - 1));
        WindowSpec hugeBackSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", hugeBack)));
        Relation hugeBackOut = new WindowEngine().execute(in, hugeBackSpec);
        t.eq(new Object[]{null, null, null, null, null}, col(hugeBackOut, "s"),
                "超大回溯偏移：终点恒在分区之前，所有帧均为空 -> NULL（无 long 回绕）");

        // 整个分区只有一行：UNBOUNDED 帧 = 自身；偏移帧视越界而定
        Relation one = relation(new String[]{"p:STRING", "v:LONG"},
                new Object[]{"z", 42L});
        WindowSpec oneSpec = new WindowSpec(List.of("p"),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(
                        FunctionSpec.sum("whole", "v",
                                new Frame(FrameBound.unboundedPreceding(),
                                        FrameBound.unboundedFollowing())),
                        FunctionSpec.sum("around", "v", frame3),
                        FunctionSpec.rowNumber("rn"),
                        FunctionSpec.rank("rk")));
        Relation oneOut = new WindowEngine().execute(one, oneSpec);
        t.eq(42L, oneOut.rows().get(0).get(oneOut.columnIndex("whole")),
                "单行分区全分区帧 = 自身");
        t.eq(42L, oneOut.rows().get(0).get(oneOut.columnIndex("around")),
                "单行分区居中三行帧截断为自身");
        t.eq(1L, oneOut.rows().get(0).get(oneOut.columnIndex("rn")),
                "单行分区 ROW_NUMBER = 1");
        t.eq(1L, oneOut.rows().get(0).get(oneOut.columnIndex("rk")),
                "单行分区 RANK = 1");

        // NULL 参与：SUM 忽略 NULL；全 NULL 帧 -> NULL
        Relation withNulls = relation(new String[]{"v:LONG"},
                new Object[]{1L}, new Object[]{null}, new Object[]{2L}, new Object[]{null});
        WindowSpec nnSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v",
                        new Frame(FrameBound.unboundedPreceding(), FrameBound.currentRow()))));
        Relation nnOut = new WindowEngine().execute(withNulls, nnSpec);
        // 排序后：1,2,null,null；帧为从分区起点到当前行：
        // 累计和分别为 1、3、3（SUM 忽略 NULL，帧内仍有 1,2）、3
        t.eq(new Object[]{1L, 3L, 3L, 3L}, col(nnOut, "s"),
                "SUM 忽略 NULL：NULL 行帧内已有非空值时累计和保持不变");

        // 全 NULL 数据：任意帧结果为 NULL
        Relation allNull = relation(new String[]{"v:LONG"},
                new Object[]{null}, new Object[]{null});
        WindowSpec anSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v",
                        new Frame(FrameBound.unboundedPreceding(), FrameBound.unboundedFollowing()))));
        Relation anOut = new WindowEngine().execute(allNull, anSpec);
        t.eq(new Object[]{null, null}, col(anOut, "s"), "全 NULL 分区帧聚合为 NULL");
    }

    private static void negativeTests(TestHarness t) {
        // 负值 + 含负值的滑动求和（验证不做无符号假设、不做下溢误报）
        Relation in = relation(new String[]{"v:LONG"},
                new Object[]{-1L}, new Object[]{2L}, new Object[]{-3L}, new Object[]{4L});
        Frame f = new Frame(
                new FrameBound(BoundKind.PRECEDING, 1),
                new FrameBound(BoundKind.FOLLOWING, 1));
        WindowSpec spec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", f)));
        Relation out = new WindowEngine().execute(in, spec);
        // 按 ORDER BY v 排序后为 -3,-1,2,4：
        // [-3,-1]=-4；[-3,-1,2]=-2；[-1,2,4]=5；[2,4]=6
        t.eq(new Object[]{-4L, -2L, 5L, 6L}, col(out, "s"),
                "负值居中滑动求和正确（按排序后顺序）");

        // 累计和围绕 0 上下波动，不报错
        WindowSpec cum = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", Frame.defaultForSum())));
        Relation cumOut = new WindowEngine().execute(in, cum);
        // 排序后 -3,-1,2,4：累计 -3,-4,-2,2
        t.eq(new Object[]{-3L, -4L, -2L, 2L}, col(cumOut, "s"),
                "负值累计和：-3,-4,-2,2");

        // MIN_VALUE 参与求和：-9223372036854775808 + 9223372036854775807 = -1（数学和合法）
        Relation edge = relation(new String[]{"v:LONG"},
                new Object[]{Long.MIN_VALUE}, new Object[]{Long.MAX_VALUE});
        WindowSpec edgeSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v",
                        new Frame(FrameBound.unboundedPreceding(), FrameBound.unboundedFollowing()))));
        Relation edgeOut = new WindowEngine().execute(edge, edgeSpec);
        t.eq(-1L, edgeOut.rows().get(0).get(edgeOut.columnIndex("s")),
                "全分区帧每行都是 MIN+MAX=-1（抵消场景合法）");
        t.eq(-1L, edgeOut.rows().get(1).get(edgeOut.columnIndex("s")),
                "全分区帧每行都是 MIN+MAX=-1");
    }

    private static void overflowTests(TestHarness t) {
        // 累计和溢出：MAX_VALUE 后再来一个 1
        Relation in = relation(new String[]{"v:LONG"},
                new Object[]{Long.MAX_VALUE}, new Object[]{1L});
        WindowSpec spec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v", Frame.defaultForSum())));
        try {
            new WindowEngine().execute(in, spec);
            t.fail("正向溢出应抛 WindowException");
        } catch (WindowException e) {
            t.check(e.getMessage().contains("溢出"), "溢出异常信息含中文说明：" + e.getMessage());
            t.check(e.getMessage().contains("9223372036854775808"),
                    "异常信息给出数学和；得到：" + e.getMessage());
        }

        // 下溢：MIN_VALUE + (-1)
        Relation under = relation(new String[]{"v:LONG"},
                new Object[]{Long.MIN_VALUE}, new Object[]{-1L});
        try {
            new WindowEngine().execute(under,
                    new WindowSpec(List.of(),
                            orders("v", true, NullOrder.NULLS_LAST),
                            List.of(FunctionSpec.sum("s", "v", Frame.defaultForSum()))));
            t.fail("负向溢出应抛 WindowException");
        } catch (WindowException e) {
            t.check(e.getMessage().contains("溢出"), "负向溢出被检出：" + e.getMessage());
        }

        // 抵消场景下“朴素逐步累加会误报、数学和合法”不应报错
        // 大数 + 大数（中间极大） - 大数 = 大数
        long big = Long.MAX_VALUE - 10;
        Relation cancel = relation(new String[]{"v:LONG"},
                new Object[]{big}, new Object[]{big}, new Object[]{-big});
        WindowSpec cancelSpec = new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("s", "v",
                        new Frame(FrameBound.unboundedPreceding(), FrameBound.currentRow()))));
        Relation cancelOut = new WindowEngine().execute(cancel, cancelSpec);
        // 排序后 -big, big, big；累计 -big, 0, big
        t.eq(new Object[]{-big, 0L, big}, col(cancelOut, "s"),
                "中间不溢出的抵消场景按数学和处理，不误报");
    }

    private static void validationTests(TestHarness t) {
        Relation in = relation(new String[]{"v:LONG", "s:STRING"},
                new Object[]{1L, "a"});

        // 不存在的列
        expectIae(t, in, new WindowSpec(List.of("nope"), List.of(),
                List.of(FunctionSpec.rowNumber("rn"))), "未知分区列");

        // 输出列重名
        expectIae(t, in, new WindowSpec(List.of(), List.of(),
                List.of(FunctionSpec.rowNumber("v"))), "输出列与输入重名");

        // 两个函数输出列互相重名
        expectIae(t, in, new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.rowNumber("x"), FunctionSpec.rank("x"))),
                "输出列互相重名");

        // SUM 字符串列
        expectIae(t, in, new WindowSpec(List.of(),
                orders("v", true, NullOrder.NULLS_LAST),
                List.of(FunctionSpec.sum("x", "s", Frame.defaultForSum()))),
                "SUM 字符串列应拒绝");

        // 非法帧：start 在 end 之后
        try {
            new Frame(new FrameBound(BoundKind.FOLLOWING, 2),
                    new FrameBound(BoundKind.PRECEDING, 1));
            t.fail("非法帧定义应抛异常");
        } catch (IllegalArgumentException e) {
            t.check(true, "非法帧定义被拒绝：" + e.getMessage());
        }

        // 负偏移
        try {
            new FrameBound(BoundKind.PRECEDING, -1);
            t.fail("负偏移应抛异常");
        } catch (IllegalArgumentException e) {
            t.check(true, "负偏移被拒绝");
        }
    }

    private static void checkedLongTests(TestHarness t) {
        t.eq(5L, CheckedLong.addExact(2L, 3L), "CheckedLong 普通加法");
        t.eq(-1L, CheckedLong.addExact(Long.MIN_VALUE, Long.MAX_VALUE),
                "MIN+MAX=-1 不溢出");
        expectArithmetic(t, Long.MAX_VALUE, 1L, "正向上溢");
        expectArithmetic(t, Long.MIN_VALUE, -1L, "负向下溢");
        expectArithmetic(t, Long.MAX_VALUE / 2 + 1, Long.MAX_VALUE / 2 + 1,
                "两个大正数上溢");
    }

    private static void expectArithmetic(TestHarness t, long a, long b, String label) {
        try {
            CheckedLong.addExact(a, b);
            t.fail(label + "：应抛 ArithmeticException");
        } catch (ArithmeticException e) {
            t.check(true, label);
        }
    }

    private static void expectIae(TestHarness t, Relation in, WindowSpec spec, String label) {
        try {
            new WindowEngine().execute(in, spec);
            t.fail(label + "：应抛 IllegalArgumentException");
        } catch (IllegalArgumentException e) {
            t.check(true, label + "：" + e.getMessage());
        }
    }
}
