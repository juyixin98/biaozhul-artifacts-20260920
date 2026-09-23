package intervalindex;

/** 直接校验增强 AVL 树不变量的小工具（同包白盒）。 */
final class TreeInvariants {

    private TreeInvariants() {}

    static long verifyStartTree(IntervalStore store) {
        return AvlTree.verify(store.startTree);
    }

    static long verifyEndTree(IntervalStore store) {
        return AvlTree.verify(store.endTree);
    }

    static void verifyBoth(IntervalStore store, long expectedTotal) {
        Check.eq(AvlTree.verify(store.startTree), expectedTotal, "startTree subtree total");
        Check.eq(AvlTree.verify(store.endTree), expectedTotal, "endTree subtree total");
        Check.eq(store.startTree.totalCount(), expectedTotal, "startTree totalCount");
        Check.eq(store.endTree.totalCount(), expectedTotal, "endTree totalCount");
        Check.eq(store.startTree.distinctSize(), store.endTree.distinctSize(),
                "distinct counts agree between trees");
    }

    static void smoke() {
        AvlTree<Integer> tree = new AvlTree<>(Integer::compare, i -> i, i -> -i);
        for (int i = 0; i < 50; i++) {
            tree.insert(i);
        }
        Check.eq(AvlTree.verify(tree), 50, "smoke tree total");
        Check.eq(tree.countPrimaryLE(25), 26, "countPrimaryLE smoke");
        for (int i = 0; i < 50; i += 2) {
            Check.eq(tree.erase(i, 1), 1, "erase even key");
        }
        Check.eq(AvlTree.verify(tree), 25, "smoke tree total after erases");
        Check.eq(tree.distinctSize(), 25, "smoke distinct after erases");
        Check.eq(tree.countPrimaryLE(24), 12, "odd keys 1..23 = 12");
    }
}
