package intervalindex;

/**
 * 增强 AVL 树直接单元测试 + IntervalStore 基本行为。
 * 随机/对抗性场景放在 PropertyTest。
 */
public class AvlTreeTest {

    public static void run() {
        treeUnitTests();
        storeSequentialTests();
        duplicateTests();
        System.out.println("AvlTreeTest OK");
    }

    private static void treeUnitTests() {
        // 顺序插入（一路右旋/双旋），secondary 取 -i 以避免 trivial
        AvlTree<Integer> asc = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        for (int i = 0; i < 200; i++) {
            asc.insert(i);
        }
        Check.eq(AvlTree.verify(asc), 200, "asc tree total");
        Check.eq(asc.distinctSize(), 200, "asc distinct");
        Check.eq(asc.countPrimaryLE(0), 1, "countPrimaryLE(0)");
        Check.eq(asc.countPrimaryLE(199), 200, "countPrimaryLE(199)");
        Check.eq(asc.countPrimaryLE(-5), 0, "countPrimaryLE below");
        Check.eq(asc.countPrimaryLE(100), 101, "countPrimaryLE(100)");
        Check.eq(asc.collectInOrder().size(), 200, "inorder size");

        // 倒序插入
        AvlTree<Integer> desc = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        for (int i = 199; i >= 0; i--) {
            desc.insert(i);
        }
        Check.eq(AvlTree.verify(desc), 200, "desc tree total");
        Check.eq(desc.countPrimaryLE(100), 101, "desc countPrimaryLE(100)");

        // 重复键与部分删除
        AvlTree<Integer> dup = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        dup.insert(7);
        dup.insert(7);
        dup.insert(7);
        Check.eq(dup.getCount(7), 3, "triple insert count");
        Check.eq(dup.totalCount(), 3, "triple total");
        Check.eq(dup.erase(7, 2), 2, "erase 2 of 3");
        Check.eq(dup.getCount(7), 1, "one remains");
        Check.eq(dup.erase(7, 100), 1, "erase overshoot");
        Check.eq(dup.distinctSize(), 0, "key removed");
        Check.eq(AvlTree.verify(dup), 0, "empty tree verifies");

        // 双子节点后继替换路径：插入再删“中间”的一批
        AvlTree<Integer> mid = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        int[] vals = {50, 25, 75, 10, 30, 60, 90, 5, 15, 27, 35, 55, 65, 85, 95};
        for (int v : vals) {
            mid.insert(v);
        }
        for (int v : new int[]{25, 75, 50}) {
            Check.eq(mid.erase(v, 1), 1, "erase two-child node " + v);
            Check.eq(mid.getCount(v), 0, "gone: " + v);
        }
        Check.eq(AvlTree.verify(mid), vals.length - 3, "total after internal erases");
        Check.eq(mid.collectInOrder().size(), vals.length - 3, "distinct after internal erases");

        // 边界：空树查询
        AvlTree<Integer> empty = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        Check.eq(empty.countPrimaryLE(10), 0, "empty countPrimaryLE");
        Check.that(empty.collectOverlaps(0, 10).isEmpty(), "empty overlap");
        Check.eq(empty.erase(1, 1), 0, "erase from empty");
    }

    private static void storeSequentialTests() {
        IntervalStore store = new IntervalStore();
        for (int i = 0; i < 100; i++) {
            store.insert(i, i + 10);
        }
        TreeInvariants.verifyBoth(store, 100);
        Check.eq(store.distinctCount(), 100, "100 distinct sequential intervals");
        Check.eq(store.coverage(50), 10, "coverage at t=50");
        Check.eq(store.coverage(0), 1, "coverage at t=0");
        Check.eq(store.coverage(99), 10, "coverage at t=99: [90,100)..[99,109)");
        Check.eq(store.coverage(100), 9, "coverage at t=100: [91,101)..[99,109)");
        Check.eq(store.coverage(109), 0, "coverage after all ends");
        Check.eq(store.coverage(-1), 0, "coverage before all starts");

        IntervalStore rev = new IntervalStore();
        for (int i = 99; i >= 0; i--) {
            rev.insert(i, i + 10);
        }
        TreeInvariants.verifyBoth(rev, 100);
        Check.eq(rev.coverage(50), 10, "reverse insert coverage");

        // 删除后覆盖计数随之更新
        for (int i = 0; i < 50; i++) {
            Check.eq(store.delete(i, i + 10, 1), 1, "delete sequential " + i);
        }
        TreeInvariants.verifyBoth(store, 50);
        Check.eq(store.coverage(10), 0, "t=10: [0,10) gone, [1,11)..[9,19) deleted too");
        Check.eq(store.coverage(60), 10, "t=60 still covered by 51..59 starts");
    }

    private static void duplicateTests() {
        IntervalStore dup = new IntervalStore();
        Check.eq(dup.insert(1, 5), 1, "first insert count");
        Check.eq(dup.insert(1, 5), 2, "duplicate insert count");
        Check.eq(dup.insert(1, 5), 3, "third insert count");
        TreeInvariants.verifyBoth(dup, 3);
        Check.eq(dup.distinctCount(), 1, "one distinct with duplicates");
        Check.eq(dup.coverage(3), 3, "coverage counts duplicates");

        Check.eq(dup.delete(1, 5, 1), 1, "delete one copy");
        TreeInvariants.verifyBoth(dup, 2);
        Check.eq(dup.totalCount(), 2, "two copies remain");
        Check.eq(dup.coverage(3), 2, "coverage after partial delete");

        Check.eq(dup.delete(1, 5, 10), 2, "overshoot delete");
        TreeInvariants.verifyBoth(dup, 0);
        Check.eq(dup.delete(1, 5, 1), 0, "delete missing returns 0");

        Check.eq(dup.insert(1, 5), 1, "reinsert after full delete");
        TreeInvariants.verifyBoth(dup, 1);

        // 拒绝空区间与逆序区间
        expectInvalid(1, 1);
        expectInvalid(5, 4);
        expectInvalid(-3, -3);
        try {
            dup.delete(0, 0, 1);
            Check.fail("delete of empty interval must throw");
        } catch (IllegalArgumentException expected) {
            // expected
        }
    }

    static void expectInvalid(long lo, long hi) {
        try {
            new IntervalStore().insert(lo, hi);
            Check.fail("expected rejection of [%d,%d)".formatted(lo, hi));
        } catch (IllegalArgumentException expected) {
            // expected
        }
    }
}
