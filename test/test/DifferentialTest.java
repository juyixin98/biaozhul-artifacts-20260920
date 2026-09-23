package test;

import engine.model.ColumnType;
import engine.model.Relation;
import engine.model.Relation.Row;
import engine.window.BoundKind;
import engine.window.Frame;
import engine.window.FrameBound;
import engine.window.FunctionSpec;
import engine.window.NullOrder;
import engine.window.OrderKey;
import engine.window.WindowEngine;
import engine.window.WindowSpec;
import testutil.NaiveWindowReference;
import testutil.Rows;
import testutil.TestHarness;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * 差分测试：大量随机输入与随机窗口规格，逐行比较生产引擎与
 * {@link NaiveWindowReference} 的结果。固定随机种子，可复现。
 *
 * <p>刻意覆盖：并列键、NULL（含分区键为 NULL）、单行/空分区、
 * 帧端点越界、负值、多函数共存、多列排序、字符串与整数混排。
 */
public final class DifferentialTest {

    private static final int ITERATIONS = 300;

    public static boolean main(String[] args) {
        TestHarness t = new TestHarness("差分对拍（引擎 vs 朴素参考）");
        Random rnd = new Random(20260923L);
        int mismatchRows = 0;

        for (int iter = 0; iter < ITERATIONS; iter++) {
            Generated gen = generate(rnd);
            Relation engineOut = new WindowEngine().execute(gen.relation, gen.spec);
            Relation naiveOut = new NaiveWindowReference().run(gen.relation, gen.spec);

            if (!engineOut.columnNames().equals(naiveOut.columnNames())) {
                t.fail("迭代 " + iter + "：列名不一致");
                continue;
            }
            if (!Rows.typeNames(engineOut).equals(Rows.typeNames(naiveOut))) {
                t.fail("迭代 " + iter + "：列类型不一致");
                continue;
            }
            List<Object[]> a = Rows.canonical(engineOut);
            List<Object[]> b = Rows.canonical(naiveOut);
            if (a.size() != b.size()) {
                t.fail("迭代 " + iter + "：行数 " + a.size() + " vs " + b.size());
                continue;
            }
            for (int i = 0; i < a.size(); i++) {
                Object[] ra = a.get(i);
                Object[] rb = b.get(i);
                if (!java.util.Arrays.equals(ra, rb)) {
                    mismatchRows++;
                    if (mismatchRows <= 3) {
                        t.fail("迭代 " + iter + " 第 " + i + " 行不一致："
                                + java.util.Arrays.toString(ra) + " vs "
                                + java.util.Arrays.toString(rb));
                    }
                }
            }
        }
        t.check(mismatchRows == 0,
                mismatchRows == 0 ? ITERATIONS + " 组随机规格全部对拍一致"
                        : "共 " + mismatchRows + " 行结果不一致");

        // 极端大值：溢出判定与结果的对拍
        overflowDifferential(t, rnd);

        // 边界构造：空输入（0 行）
        Relation empty = WindowEngineTest.relation(new String[]{"p:STRING", "v:LONG"});
        WindowSpec emptySpec = new WindowSpec(List.of("p"),
                List.of(new OrderKey("v", true, NullOrder.NULLS_LAST)),
                List.of(FunctionSpec.rowNumber("rn"),
                        FunctionSpec.rank("rk"),
                        FunctionSpec.sum("s", "v", Frame.defaultForSum())));
        Relation e1 = new WindowEngine().execute(empty, emptySpec);
        Relation e2 = new NaiveWindowReference().run(empty, emptySpec);
        t.eq(0, e1.rows().size(), "空输入：引擎产出 0 行");
        t.eq(0, e2.rows().size(), "空输入：参考产出 0 行");
        t.eq(3, e1.columnCount() - 2, "空输入：结果列仍然追加");

        // 边界构造：分区键全部为 NULL（同一组）
        Relation nullKeys = WindowEngineTest.relation(new String[]{"p:STRING", "v:LONG"},
                new Object[]{null, 2L}, new Object[]{null, 1L});
        WindowSpec nkSpec = new WindowSpec(List.of("p"),
                List.of(new OrderKey("v", true, NullOrder.NULLS_LAST)),
                List.of(FunctionSpec.rowNumber("rn")));
        Relation nk1 = new WindowEngine().execute(nullKeys, nkSpec);
        Relation nk2 = new NaiveWindowReference().run(nullKeys, nkSpec);
        t.eq(2, nk1.rows().size(), "NULL 分区键归入同一分区");
        List<Object[]> c1 = Rows.canonical(nk1);
        List<Object[]> c2 = Rows.canonical(nk2);
        t.check(c1.size() == c2.size()
                        && java.util.Arrays.equals(c1.get(0), c2.get(0))
                        && java.util.Arrays.equals(c1.get(1), c2.get(1)),
                "NULL 分区键两实现一致");

        return t.report();
    }

    // ------------------------------------------------------------------

    private record Generated(Relation relation, WindowSpec spec) {
    }

    /**
     * 溢出对拍：用极端大值（含正负号、成对抵消）生成数据，比较两个实现
     * “是否抛 WindowException”以及成功时的逐行结果是否一致。
     */
    private static void overflowDifferential(TestHarness t, Random rnd) {
        int cases = 120;
        int bothThrow = 0;
        int bothOk = 0;
        long[] extremes = {
                Long.MAX_VALUE, Long.MIN_VALUE, Long.MAX_VALUE - 1,
                Long.MIN_VALUE + 1, -(Long.MAX_VALUE / 2), Long.MAX_VALUE / 2,
                1L << 40, -(1L << 40)};
        for (int iter = 0; iter < cases; iter++) {
            int n = 1 + rnd.nextInt(6);
            List<Row> rows = new ArrayList<>();
            for (int r = 0; r < n; r++) {
                Object v = rnd.nextInt(10) < 8 ? extremes[rnd.nextInt(extremes.length)]
                        : (long) (rnd.nextInt(7) - 3);
                rows.add(new Row(r, new Object[]{v}));
            }
            Relation rel = new Relation(List.of("c0"), List.of(ColumnType.LONG), rows);
            Frame[] frames = {
                    Frame.defaultForSum(),
                    new Frame(FrameBound.unboundedPreceding(), FrameBound.unboundedFollowing()),
                    new Frame(new FrameBound(BoundKind.PRECEDING, 1),
                            new FrameBound(BoundKind.FOLLOWING, 1))
            };
            WindowSpec spec = new WindowSpec(List.of(),
                    List.of(new OrderKey("c0", true, NullOrder.NULLS_LAST)),
                    List.of(FunctionSpec.sum("s", "c0", frames[rnd.nextInt(frames.length)])));

            boolean engineThrew = false;
            boolean naiveThrew = false;
            Relation engineOut = null;
            Relation naiveOut = null;
            try {
                engineOut = new WindowEngine().execute(rel, spec);
            } catch (engine.window.WindowException e) {
                engineThrew = true;
            }
            try {
                naiveOut = new NaiveWindowReference().run(rel, spec);
            } catch (engine.window.WindowException e) {
                naiveThrew = true;
            }
            if (engineThrew != naiveThrew) {
                t.fail("溢出对拍迭代 " + iter + "：引擎抛异常=" + engineThrew
                        + "，参考抛异常=" + naiveThrew);
                continue;
            }
            if (engineThrew) {
                bothThrow++;
                continue;
            }
            bothOk++;
            List<Object[]> a = Rows.canonical(engineOut);
            List<Object[]> b = Rows.canonical(naiveOut);
            for (int i = 0; i < a.size(); i++) {
                if (!java.util.Arrays.equals(a.get(i), b.get(i))) {
                    t.fail("溢出对拍迭代 " + iter + " 第 " + i + " 行："
                            + java.util.Arrays.toString(a.get(i)) + " vs "
                            + java.util.Arrays.toString(b.get(i)));
                }
            }
        }
        t.check(bothThrow + bothOk == cases,
                "溢出对拍 " + cases + " 组：两实现均溢出 " + bothThrow
                        + " 组、均成功 " + bothOk + " 组，行为完全一致");
    }

    private static Generated generate(Random rnd) {
        // 1..4 列：至少一个 LONG（SUM 参数用），其余随机类型
        int colCount = 1 + rnd.nextInt(3);
        List<String> names = new ArrayList<>();
        List<ColumnType> types = new ArrayList<>();
        names.add("c0");
        types.add(ColumnType.LONG);
        for (int c = 1; c < colCount; c++) {
            names.add("c" + c);
            types.add(rnd.nextBoolean() ? ColumnType.LONG : ColumnType.STRING);
        }

        // 0..30 行（空关系、单行、小分区都要覆盖）
        int rowCount = rnd.nextInt(31);
        List<Row> rows = new ArrayList<>(rowCount);
        // 小取值域制造大量并列与 NULL
        String[] strDomain = {"a", "b", "c", "a", null};
        for (int r = 0; r < rowCount; r++) {
            Object[] vals = new Object[colCount];
            for (int c = 0; c < colCount; c++) {
                if (rnd.nextInt(100) < 25) {
                    vals[c] = null;
                } else if (types.get(c) == ColumnType.LONG) {
                    // 小整数 + 偶尔负值 + 偶尔边界值
                    int mode = rnd.nextInt(10);
                    vals[c] = switch (mode) {
                        case 0 -> (long) rnd.nextInt(7) - 3;
                        case 1 -> -Math.abs((long) rnd.nextInt(1000));
                        case 2 -> (long) rnd.nextInt(1000);
                        default -> (long) rnd.nextInt(5) - 2;
                    };
                } else {
                    vals[c] = strDomain[rnd.nextInt(strDomain.length)];
                }
            }
            rows.add(new Row(r, vals));
        }
        Relation rel = new Relation(names, types, rows);

        // 随机分区键/排序键
        List<String> partBy = pickColumns(rnd, names, 2);
        List<OrderKey> orderBy = new ArrayList<>();
        for (String c : pickColumns(rnd, names, 2)) {
            boolean asc = rnd.nextBoolean();
            NullOrder no = rnd.nextBoolean()
                    ? (asc ? NullOrder.NULLS_LAST : NullOrder.NULLS_FIRST)
                    : (rnd.nextBoolean() ? NullOrder.NULLS_FIRST : NullOrder.NULLS_LAST);
            orderBy.add(new OrderKey(c, asc, no));
        }

        List<FunctionSpec> fns = new ArrayList<>();
        fns.add(FunctionSpec.rowNumber("out_rn"));
        if (rnd.nextBoolean()) {
            fns.add(FunctionSpec.rank("out_rk"));
        }
        if (rnd.nextInt(100) < 80) {
            fns.add(FunctionSpec.sum("out_sum", "c0", randomFrame(rnd)));
        }
        return new Generated(rel, new WindowSpec(partBy, orderBy, fns));
    }

    private static List<String> pickColumns(Random rnd, List<String> all, int max) {
        List<String> pool = new ArrayList<>(all);
        List<String> picked = new ArrayList<>();
        int n = rnd.nextInt(Math.min(max, pool.size()) + 1);
        for (int i = 0; i < n; i++) {
            picked.add(pool.remove(rnd.nextInt(pool.size())));
        }
        return picked;
    }

    private static Frame randomFrame(Random rnd) {
        FrameBound start;
        FrameBound end;
        int tries = 0;
        do {
            start = randomBound(rnd, true);
            end = randomBound(rnd, false);
            tries++;
        } while (start.relativePosition() > end.relativePosition() && tries < 20);
        if (start.relativePosition() > end.relativePosition()) {
            return Frame.defaultForSum();
        }
        return new Frame(start, end);
    }

    private static FrameBound randomBound(Random rnd, boolean startSide) {
        BoundKind[] kinds = startSide
                ? new BoundKind[]{BoundKind.UNBOUNDED_PRECEDING, BoundKind.PRECEDING, BoundKind.CURRENT_ROW}
                : new BoundKind[]{BoundKind.CURRENT_ROW, BoundKind.FOLLOWING, BoundKind.UNBOUNDED_FOLLOWING};
        BoundKind kind = kinds[rnd.nextInt(kinds.length)];
        if (kind == BoundKind.PRECEDING || kind == BoundKind.FOLLOWING) {
            long off = rnd.nextInt(4); // 0..3：0 会被解析式归一化，这里直接构造 CURRENT ROW
            if (off == 0) {
                return FrameBound.currentRow();
            }
            return new FrameBound(kind, off);
        }
        return new FrameBound(kind, 0);
    }
}
