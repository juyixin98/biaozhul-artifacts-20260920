package intervalindex;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.function.ToLongFunction;

/**
 * 区间多重集的增强 AVL 平衡二叉搜索树。
 *
 * <p>每个键 K 在树中至多占一个节点，节点携带 {@code count}（相同区间的重复份数）。
 * 除标准 AVL 高度外，每个子树额外维护：
 * <ul>
 *   <li>{@code subtreeTotal}：子树内区间份数总和（含重复）；</li>
 *   <li>{@code augMaxSecondary}：子树内区间“第二端点”（start 树为右端点 hi，end 树为 lo）的最大值；</li>
 *   <li>{@code minPrimary} / {@code maxPrimary}：子树内第一端点（start 树为 lo，end 树为 hi）的最小/最大值。</li>
 * </ul>
 *
 * <p>键顺序由 {@code comparator} 给定（{@code (lo,hi)} 字典序，或反向字典序），
 * {@code primary} 取键的第一分量，{@code secondary} 取键的第二分量（子树最大值聚合对象）。
 * 插入、删除、计数均为 O(log n)；重叠查询利用 max-secondary 剪枝；
 * 覆盖计数利用 subtreeTotal 与 primary 边界在 O(log n) 内完成。
 */
final class AvlTree<K> {

    static final class Node<K> {
        K key;
        long count;                 // 该键的重复份数
        long subtreeTotal;          // 子树份数总和
        long augMaxSecondary;       // 子树第二端点最大值
        long minPrimary;            // 子树第一端点最小值
        long maxPrimary;            // 子树第一端点最大值
        int height;
        Node<K> left, right;

        Node(K key, long primary, long secondary) {
            this.key = key;
            this.count = 1;
            this.subtreeTotal = 1;
            this.augMaxSecondary = secondary;
            this.minPrimary = primary;
            this.maxPrimary = primary;
            this.height = 1;
        }
    }

    private final Comparator<? super K> comparator;
    private final ToLongFunction<K> primary;
    private final ToLongFunction<K> secondary;

    Node<K> root;
    private long distinct; // 不同键的个数

    AvlTree(Comparator<? super K> comparator,
            ToLongFunction<K> primary,
            ToLongFunction<K> secondary) {
        this.comparator = comparator;
        this.primary = primary;
        this.secondary = secondary;
    }

    long distinctSize() {
        return distinct;
    }

    /** 树内区间总份数（含重复）。 */
    long totalCount() {
        return root == null ? 0 : root.subtreeTotal;
    }

    long getCount(K key) {
        Node<K> n = find(root, key);
        return n == null ? 0 : n.count;
    }

    /** 插入一份 key，返回该键当前份数。 */
    long insert(K key) {
        long[] after = new long[1];
        root = insertRec(root, key, after);
        return after[0];
    }

    private Node<K> insertRec(Node<K> node, K key, long[] after) {
        if (node == null) {
            distinct++;
            Node<K> created = new Node<>(key, primary.applyAsLong(key), secondary.applyAsLong(key));
            after[0] = 1;
            return created;
        }
        int cmp = comparator.compare(key, node.key);
        if (cmp < 0) {
            node.left = insertRec(node.left, key, after);
        } else if (cmp > 0) {
            node.right = insertRec(node.right, key, after);
        } else {
            node.count++;
            node.subtreeTotal++;
            long sec = secondary.applyAsLong(node.key);
            if (sec > node.augMaxSecondary) {
                node.augMaxSecondary = sec;
            }
            after[0] = node.count;
            return node;
        }
        return recomputeAndBalance(node);
    }

    /**
     * 删除至多 {@code amount} 份 key。
     * 返回实际删除份数（0 表示该键不存在）。
     */
    long erase(K key, long amount) {
        if (amount <= 0) {
            return 0;
        }
        long[] removed = new long[1];
        root = eraseRec(root, key, amount, removed, false);
        return removed[0];
    }

    private Node<K> eraseRec(Node<K> node, K key, long amount, long[] removed, boolean removeEntire) {
        if (node == null) {
            return null;
        }
        int cmp = comparator.compare(key, node.key);
        if (cmp < 0) {
            node.left = eraseRec(node.left, key, amount, removed, removeEntire);
            return recomputeAndBalance(node);
        }
        if (cmp > 0) {
            node.right = eraseRec(node.right, key, amount, removed, removeEntire);
            return recomputeAndBalance(node);
        }

        long take = removeEntire ? node.count : Math.min(amount, node.count);
        node.count -= take;
        removed[0] += take;
        if (node.count > 0) {
            // 仍有剩余份数：节点保留，仅更新聚合
            return recomputeAndBalance(node);
        }

        // 节点从树中物理移除
        if (node.left == null) {
            distinct--;
            return node.right;
        }
        if (node.right == null) {
            distinct--;
            return node.left;
        }
        // 双子节点：用中序后继（右子树最小节点）替换当前节点。
        // distinct 净效果恰为“删除一个键”：后继在递归中被物理删除（distinct--），
        // 当前节点键被后继内容顶替，不额外计数。后继份数只是搬到当前位置，
        // 不能计入本次调用的 removed，故传入独立的 dummy 数组。
        Node<K> succ = minNode(node.right);
        node.key = succ.key;
        node.count = succ.count;
        node.right = eraseRec(node.right, succ.key, Long.MAX_VALUE, new long[1], true);
        return recomputeAndBalance(node);
    }

    private static <K> Node<K> minNode(Node<K> node) {
        while (node.left != null) {
            node = node.left;
        }
        return node;
    }

    private Node<K> find(Node<K> node, K key) {
        while (node != null) {
            int cmp = comparator.compare(key, node.key);
            if (cmp < 0) {
                node = node.left;
            } else if (cmp > 0) {
                node = node.right;
            } else {
                return node;
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // 增强查询
    // ------------------------------------------------------------------

    /**
     * 子树内“第一端点 ≤ t”的区间份数总和。
     * start 树用它统计 lo ≤ t，end 树用它统计 hi ≤ t。O(log n)。
     */
    long countPrimaryLE(long t) {
        long total = 0;
        Node<K> n = root;
        while (n != null) {
            long p = primary.applyAsLong(n.key);
            if (p <= t) {
                total += n.count + (n.left == null ? 0 : n.left.subtreeTotal);
                n = n.right;
            } else {
                n = n.left;
            }
        }
        return total;
    }

    /**
     * 收集所有与半开区间 [lo, hi) 相交的键。
     *
     * <p>start 树按 (lo,hi) 字典序排列。两区间相交当且仅当 lo₁ &lt; hi₂ 且 lo₂ &lt; hi₁。
     * 剪枝（均为“保守跳过”，绝不会漏掉相交区间）：
     * <ul>
     *   <li>子树最大右端点 augMaxSecondary ≤ lo：子树内区间全部 end ≤ lo，不可能相交；</li>
     *   <li>子树最小左端点 minPrimary ≥ hi：子树内区间全部 start ≥ hi，不可能相交。</li>
     * </ul>
     * 输出按 (lo, hi) 字典序（中序）。
     */
    List<K> collectOverlaps(long lo, long hi) {
        List<K> out = new ArrayList<>();
        overlapRec(root, lo, hi, out);
        return out;
    }

    private void overlapRec(Node<K> node, long lo, long hi, List<K> out) {
        if (node == null) {
            return;
        }
        if (node.augMaxSecondary <= lo) {
            return;
        }
        if (node.minPrimary >= hi) {
            return;
        }
        overlapRec(node.left, lo, hi, out);
        long nodeLo = primary.applyAsLong(node.key);
        long nodeHi = secondary.applyAsLong(node.key);
        if (nodeLo < hi && nodeHi > lo) { // 半开：相邻端点不相交
            out.add(node.key);
        }
        overlapRec(node.right, lo, hi, out);
    }

    /** 中序收集全部键（按 comparator 顺序）。 */
    List<K> collectInOrder() {
        List<K> out = new ArrayList<>();
        inOrder(root, out);
        return out;
    }

    private void inOrder(Node<K> node, List<K> out) {
        if (node == null) {
            return;
        }
        inOrder(node.left, out);
        out.add(node.key);
        inOrder(node.right, out);
    }

    // ------------------------------------------------------------------
    // AVL 平衡
    // ------------------------------------------------------------------

    private Node<K> recomputeAndBalance(Node<K> n) {
        recompute(n);
        int bf = balanceFactor(n);
        if (bf > 1) {
            if (balanceFactor(n.left) < 0) {
                n.left = rotateLeft(n.left);
            }
            return rotateRight(n);
        }
        if (bf < -1) {
            if (balanceFactor(n.right) > 0) {
                n.right = rotateRight(n.right);
            }
            return rotateLeft(n);
        }
        return n;
    }

    private Node<K> rotateRight(Node<K> y) {
        Node<K> x = y.left;
        Node<K> t2 = x.right;
        x.right = y;
        y.left = t2;
        recompute(y);
        recompute(x);
        return x;
    }

    private Node<K> rotateLeft(Node<K> x) {
        Node<K> y = x.right;
        Node<K> t2 = y.left;
        y.left = x;
        x.right = t2;
        recompute(x);
        recompute(y);
        return y;
    }

    private void recompute(Node<K> n) {
        n.height = 1 + Math.max(height(n.left), height(n.right));
        long total = n.count;
        long maxSec = secondary.applyAsLong(n.key);
        long minP = primary.applyAsLong(n.key);
        long maxP = minP;
        if (n.left != null) {
            total += n.left.subtreeTotal;
            maxSec = Math.max(maxSec, n.left.augMaxSecondary);
            minP = Math.min(minP, n.left.minPrimary);
            maxP = Math.max(maxP, n.left.maxPrimary);
        }
        if (n.right != null) {
            total += n.right.subtreeTotal;
            maxSec = Math.max(maxSec, n.right.augMaxSecondary);
            minP = Math.min(minP, n.right.minPrimary);
            maxP = Math.max(maxP, n.right.maxPrimary);
        }
        n.subtreeTotal = total;
        n.augMaxSecondary = maxSec;
        n.minPrimary = minP;
        n.maxPrimary = maxP;
    }

    private int height(Node<K> n) {
        return n == null ? 0 : n.height;
    }

    private int balanceFactor(Node<K> n) {
        return n == null ? 0 : height(n.left) - height(n.right);
    }

    // ---- 测试用：不变量校验 ----

    /** 递归校验 BST 顺序、AVL 高度、subtreeTotal 与全部增强值；返回子树总份数。 */
    static <K> long verify(AvlTree<K> tree) {
        if (tree.root == null) {
            return 0;
        }
        return verifyRec(tree, tree.root, null, null);
    }

    private static <K> long verifyRec(AvlTree<K> tree, Node<K> n, K low, K high) {
        if (n == null) {
            return 0;
        }
        if (low != null && tree.comparator.compare(low, n.key) >= 0) {
            throw new AssertionError("BST order violated (low bound) at " + n.key);
        }
        if (high != null && tree.comparator.compare(n.key, high) >= 0) {
            throw new AssertionError("BST order violated (high bound) at " + n.key);
        }
        long total = n.count + verifyRec(tree, n.left, low, n.key)
                + verifyRec(tree, n.right, n.key, high);
        if (total != n.subtreeTotal) {
            throw new AssertionError("subtreeTotal mismatch at " + n.key);
        }
        long selfSec = tree.secondary.applyAsLong(n.key);
        long selfP = tree.primary.applyAsLong(n.key);
        long expectAug = selfSec;
        long expectMin = selfP;
        long expectMax = selfP;
        if (n.left != null) {
            expectAug = Math.max(expectAug, n.left.augMaxSecondary);
            expectMin = Math.min(expectMin, n.left.minPrimary);
            expectMax = Math.max(expectMax, n.left.maxPrimary);
        }
        if (n.right != null) {
            expectAug = Math.max(expectAug, n.right.augMaxSecondary);
            expectMin = Math.min(expectMin, n.right.minPrimary);
            expectMax = Math.max(expectMax, n.right.maxPrimary);
        }
        if (expectAug != n.augMaxSecondary) {
            throw new AssertionError("augMaxSecondary mismatch at " + n.key);
        }
        if (expectMin != n.minPrimary || expectMax != n.maxPrimary) {
            throw new AssertionError("min/max primary mismatch at " + n.key);
        }
        int h = 1 + Math.max(tree.height(n.left), tree.height(n.right));
        if (h != n.height) {
            throw new AssertionError("height mismatch at " + n.key);
        }
        int bf = tree.height(n.left) - tree.height(n.right);
        if (Math.abs(bf) > 1) {
            throw new AssertionError("AVL balance violated at " + n.key);
        }
        return total;
    }
}
