package phj.join;

import phj.Test;
import phj.TestRunner;
import phj.core.JoinType;
import phj.core.Key;
import phj.core.QueryResult;
import phj.core.Relation;
import phj.core.Row;
import phj.core.Value;

import java.util.List;

public class EngineSemanticsTest {

    private static Relation L(Object[]... rows) {
        return TestUtil.relation("L", List.of("id", "k", "v"), rows);
    }

    private static Relation R(Object[]... rows) {
        return TestUtil.relation("R", List.of("cid", "k", "w"), rows);
    }

    private static QueryResult run(Relation l, Relation r, JoinType t, long threshold, int parts) {
        return run(l, r, t, threshold, parts, -1);
    }

    private static QueryResult run(Relation l, Relation r, JoinType t, long threshold, int parts, long quota) {
        return TestUtil.assertAgainstReference(
                TestUtil.request(l, r, t, List.of("k"), threshold, parts, quota));
    }

    // ----------------------------------------------------------- Value 语义

    @Test
    public void valueNullNeverEqualsNull() {
        TestRunner.assertFalse(Value.NULL.equals(Value.NULL));
        TestRunner.assertFalse(Value.ofLong(1).equals(Value.NULL));
        TestRunner.assertTrue(Value.ofString("a").equals(Value.ofString("a")));
        TestRunner.assertFalse(Value.ofString("1").equals(Value.ofLong(1)));
        TestRunner.assertFalse(Value.ofBool(true).equals(Value.ofLong(1)));
    }

    @Test
    public void numericCrossTypeEquality() {
        TestRunner.assertTrue(Value.ofLong(1).equals(Value.ofDouble(1.0)));
        TestRunner.assertEquals(Value.ofLong(1).hashCode(), Value.ofDouble(1.0).hashCode());
        TestRunner.assertTrue(Value.ofDouble(2.0).equals(Value.ofLong(2)));
        TestRunner.assertFalse(Value.ofDouble(1.5).equals(Value.ofLong(1)));
        // 相等对象必须哈希相同（哈希表契约）
        TestRunner.assertEquals(Value.ofDouble(2.0).hashCode(), Value.ofLong(2).hashCode());
        // +0.0 / -0.0 在连接语义里相等，且与 long 0 哈希一致
        TestRunner.assertTrue(Value.ofDouble(0.0).equals(Value.ofDouble(-0.0)));
        TestRunner.assertEquals(Value.ofLong(0).hashCode(), Value.ofDouble(-0.0).hashCode());
        TestRunner.assertEquals(Value.ofLong(0).hashCode(), Value.ofDouble(0.0).hashCode());
    }

    @Test
    public void multiColumnKeySemantics() {
        Relation l = TestUtil.relation("L", List.of("a", "b"),
                new Object[]{1, "x"}, new Object[]{2, "x"}, new Object[]{1, null});
        Relation r = TestUtil.relation("R", List.of("a", "b"),
                new Object[]{1, "x"}, new Object[]{1, "y"}, new Object[]{1, null});
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("a", "b"), 100, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        // 仅 (1,"x")=(1,"x") 一条；NULL 行不匹配
        TestRunner.assertEquals(1, qr.rows.size());
        TestRunner.assertEquals(TestUtil.row(1, "x", 1, "x"), qr.rows.get(0));
    }

    // ----------------------------------------------------------- 基本连接语义

    @Test
    public void innerBasicInMemory() {
        QueryResult qr = run(
                L(new Object[]{1, "a", 10}, new Object[]{2, "b", 20}, new Object[]{3, null, 30}),
                R(new Object[]{10, "a", "X"}, new Object[]{11, "c", "Y"}, new Object[]{12, null, "Z"}),
                JoinType.INNER, 100, 4);
        TestRunner.assertEquals(1, qr.rows.size());
        TestRunner.assertEquals(TestUtil.row(1, "a", 10, 10, "a", "X"), qr.rows.get(0));
    }

    @Test
    public void leftBasicInMemory() {
        Relation l = L(new Object[]{1, "a", 10}, new Object[]{2, "b", 20},
                new Object[]{3, null, 30}, new Object[]{4, "z", 40});
        Relation r = R(new Object[]{10, "a", "X"});
        QueryResult qr = run(l, r, JoinType.LEFT, 100, 4);
        // a 匹配一条；b / z / null 均补 NULL 行
        TestRunner.assertEquals(4, qr.rows.size());
    }

    @Test
    public void duplicateKeysCartesianProduct() {
        Relation l = L(new Object[]{1, "a", 1}, new Object[]{2, "a", 2}, new Object[]{3, "a", 3});
        Relation r = R(new Object[]{10, "a", "X"}, new Object[]{11, "a", "Y"});
        QueryResult qr = run(l, r, JoinType.INNER, 100, 4);
        TestRunner.assertEquals(6, qr.rows.size()); // 3 x 2
        TestRunner.assertEquals(6L, qr.stats.get("outputRows"));
    }

    @Test
    public void duplicateKeysSpillAndFallback() {
        // 阈值=1 + 单一热点键 -> BNL 回退，必须仍是笛卡尔积
        Relation l = L(new Object[]{1, "a", 1}, new Object[]{2, "a", 2}, new Object[]{3, "a", 3},
                new Object[]{4, "a", 4});
        Relation r = R(new Object[]{10, "a", "X"}, new Object[]{11, "a", "Y"}, new Object[]{12, "a", "Z"});
        QueryResult qr = run(l, r, JoinType.INNER, 1, 4, -1);
        TestRunner.assertEquals(12, qr.rows.size()); // 4 x 3
        TestRunner.assertTrue((Long) qr.stats.get("boundedFallbacks") >= 1, "应至少发生一次有界回退");
    }

    @Test
    public void nullKeySemanticsBothSides() {
        Relation l = L(new Object[]{1, null, 1}, new Object[]{2, null, 2}, new Object[]{3, "a", 3});
        Relation r = R(new Object[]{10, null, "X"}, new Object[]{11, "a", "Y"});
        // INNER：NULL=NULL 不匹配，只留 a
        QueryResult inner = run(l, r, JoinType.INNER, 100, 4);
        TestRunner.assertEquals(1, inner.rows.size());
        // LEFT：两条左 NULL 行各补 NULL，a 匹配
        QueryResult left = run(l, r, JoinType.LEFT, 100, 4);
        TestRunner.assertEquals(3, left.rows.size());
        long nullPad = left.rows.stream().filter(x -> x.get(3).isNull()).count();
        TestRunner.assertEquals(2L, nullPad);
    }

    @Test
    public void emptyTables() {
        Relation nonEmpty = L(new Object[]{1, "a", 1});
        Relation emptyL = new Relation("L", List.of("id", "k", "v"), List.of());
        Relation emptyR = new Relation("R", List.of("cid", "k", "w"), List.of());

        // INNER 任一表空 -> 空
        TestRunner.assertEquals(0, run(emptyL, nonEmpty, JoinType.INNER, 4, 4).rows.size());
        TestRunner.assertEquals(0, run(nonEmpty, emptyR, JoinType.INNER, 4, 4).rows.size());
        TestRunner.assertEquals(0, run(emptyL, emptyR, JoinType.INNER, 4, 4).rows.size());
        // LEFT 右表空 -> 左表全部补 NULL
        QueryResult left = run(nonEmpty, emptyR, JoinType.LEFT, 4, 4);
        TestRunner.assertEquals(1, left.rows.size());
        TestRunner.assertTrue(left.rows.get(0).get(3).isNull());
        // LEFT 左表空 -> 空
        TestRunner.assertEquals(0, run(emptyL, nonEmpty, JoinType.LEFT, 4, 4).rows.size());
    }

    @Test
    public void allNullKeys() {
        Relation l = L(new Object[]{1, null, 1}, new Object[]{2, null, 2});
        Relation r = R(new Object[]{10, null, "X"});
        // 需要落盘的体量，但全是 NULL 键：不产生任何分区写入
        QueryResult inner = run(l, r, JoinType.INNER, 1, 4, -1);
        TestRunner.assertEquals(0, inner.rows.size());
        QueryResult left = run(l, r, JoinType.LEFT, 1, 4, -1);
        TestRunner.assertEquals(2, left.rows.size());
        TestRunner.assertEquals(0L, left.stats.get("spillPeakBytes"));
    }

    @Test
    public void numericKeysLongAndDouble() {
        Relation l = TestUtil.relation("L", List.of("id", "k"),
                new Object[]{1, 1L}, new Object[]{2, 2L}, new Object[]{3, 3L});
        Relation r = TestUtil.relation("R", List.of("cid", "k"),
                new Object[]{10, 1.0}, new Object[]{11, 2.0}, new Object[]{12, 4.0});
        QueryResult qr = run(l, r, JoinType.INNER, 1, 4, -1);
        TestRunner.assertEquals(2, qr.rows.size()); // 1=1.0, 2=2.0
    }

    @Test
    public void innerPicksSmallerSideAsBuild() {
        // 左大右小：INNER 应选右表为建表侧；结果多重集与参考一致即语义正确
        Relation l = L();
        java.util.List<Object[]> lrows = new java.util.ArrayList<>();
        for (int i = 0; i < 100; i++) lrows.add(new Object[]{i, i % 7, i});
        l = TestUtil.relation("L", List.of("id", "k", "v"), lrows.toArray(Object[][]::new));
        Relation r = R(new Object[]{10, 0L, "X"}, new Object[]{11, 6L, "Y"});
        QueryResult qr = run(l, r, JoinType.INNER, 2, 4, -1);
        // 100 行里 k=0 或 k=6 各 ~14-15 个
        TestRunner.assertTrue(qr.rows.size() >= 28, "应当有 28+ 匹配，实际 " + qr.rows.size());
        TestRunner.assertEquals("right", planBuildSide(qr));
    }

    @Test
    public void leftAlwaysBuildsRight() {
        Relation l = L(new Object[]{1, "a", 1}, new Object[]{2, "b", 2});
        Relation r = R();
        java.util.List<Object[]> rrows = new java.util.ArrayList<>();
        for (int i = 0; i < 50; i++) rrows.add(new Object[]{100 + i, i % 5L, "w" + i});
        r = TestUtil.relation("R", List.of("cid", "k", "w"), rrows.toArray(Object[][]::new));
        QueryResult qr = run(l, r, JoinType.LEFT, 2, 4, -1);
        TestRunner.assertEquals("right", planBuildSide(qr));
        // 两个左行都至少出现一次（匹配或补 NULL）
        TestRunner.assertEquals(2, qr.rows.size());
    }

    private static String planBuildSide(QueryResult qr) {
        Object bs = qr.planRoot.toMap().get("buildSide");
        return bs == null ? null : bs.toString();
    }

    @Test
    public void unmatchedLeftRowsHaveFullNullPadding() {
        Relation l = L(new Object[]{1, "nope", 1});
        Relation r = R(new Object[]{10, "a", "X"}, new Object[]{11, "b", "Y"});
        QueryResult qr = run(l, r, JoinType.LEFT, 1, 2, -1);
        Row out = qr.rows.get(0);
        TestRunner.assertEquals(6, out.width());
        for (int i = 3; i < 6; i++) TestRunner.assertTrue(out.get(i).isNull(), "右列补位应为 NULL");
    }
}
