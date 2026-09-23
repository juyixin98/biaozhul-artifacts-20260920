package com.example.intervalindex.core;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 增强红黑树实现的区间索引。
 *
 * <p>BST 键为三元组 {@code (start, end, id)}，因此允许相同区间、相同起点
 * （重复区间靠唯一 id 区分）；每个节点额外维护两个子树聚合信息：
 * <ul>
 *   <li>{@code size}：子树节点数；</li>
 *   <li>{@code maxEnd}：子树中所有区间右端点的最大值。</li>
 * </ul>
 *
 * <p>利用 {@code maxEnd} 剪枝：子树 {@code maxEnd <= lo} 时，其中不可能存在
 * 与 [lo, hi) 相交（或覆盖 lo）的区间。所有区间统一为左闭右开。
 *
 * <p>旋转/插入/删除后沿祖先链重算聚合值，更新代价 O(log n)。
 * 公开方法均加锁，可被 HttpServer 的多线程 Executor 并发调用。
 */
public class IntervalIndex {

    private static final boolean RED = true;
    private static final boolean BLACK = false;

    private static final class Node {
        final long id;
        long start;
        long end;
        Node left;
        Node right;
        Node parent;
        boolean color;
        int size;
        long maxEnd;

        Node(long id, long start, long end, Node nil) {
            this.id = id;
            this.start = start;
            this.end = end;
            this.left = nil;
            this.right = nil;
            this.parent = nil;
            this.color = RED;
            this.size = 1;
            this.maxEnd = end;
        }

        /** 仅用于构造 nil 哨兵。 */
        Node() {
            this.id = -1;
            this.color = BLACK;
            this.size = 0;
            this.maxEnd = Long.MIN_VALUE;
        }
    }

    private final Node nil = new Node();
    private Node root = nil;
    private final Map<Long, Node> byId = new HashMap<>();
    private long nextId = 1;

    public synchronized int size() {
        return root.size;
    }

    /** 插入区间，返回服务端分配的唯一 id。start/end 相同的区间可重复插入。 */
    public synchronized long insert(long start, long end) {
        if (start >= end) {
            throw new IllegalArgumentException(
                    "invalid half-open interval [" + start + ", " + end
                            + "): require start < end");
        }
        long id = nextId++;
        Node z = new Node(id, start, end, nil);
        Node y = nil;
        Node x = root;
        while (x != nil) {
            y = x;
            if (less(z, x)) {
                x = x.left;
            } else {
                x = x.right;
            }
        }
        z.parent = y;
        if (y == nil) {
            root = z;
        } else if (less(z, y)) {
            y.left = z;
        } else {
            y.right = z;
        }
        byId.put(id, z);
        insertFixup(z);
        pullChain(z);
        return id;
    }

    /** 按 id 删除；存在并删除返回 true，id 不存在返回 false。 */
    public synchronized boolean delete(long id) {
        Node z = byId.get(id);
        if (z == null) {
            return false;
        }
        byId.remove(id);

        Node y = z;
        boolean yOriginalRed = y.color;
        Node x;
        if (z.left == nil) {
            x = z.right;
            transplant(z, z.right);
        } else if (z.right == nil) {
            x = z.left;
            transplant(z, z.left);
        } else {
            y = minimum(z.right);
            yOriginalRed = y.color;
            x = y.right;
            if (y.parent == z) {
                // x 可能是 nil；transplant 不发生，但要保证 fixup 能找到父节点
                x.parent = y;
            } else {
                transplant(y, y.right);
                y.right = z.right;
                y.right.parent = y;
            }
            transplant(z, y);
            y.left = z.left;
            y.left.parent = y;
            y.color = z.color;
        }
        if (!yOriginalRed) {
            deleteFixup(x);
        }
        if (root != nil) {
            Node anchor = (x != nil) ? x : x.parent;
            if (anchor == nil || anchor == null) {
                anchor = root;
            }
            pullChain(anchor);
        }
        return true;
    }

    /** 是否存在任意与 [lo, hi) 相交的区间。 */
    public synchronized boolean anyOverlap(long lo, long hi) {
        validateQuery(lo, hi);
        Node x = root;
        while (x != nil) {
            if (x.start < hi && x.end > lo) {
                return true;
            }
            if (x.left != nil && x.left.maxEnd > lo) {
                x = x.left;
            } else {
                x = x.right;
            }
        }
        return false;
    }

    /** 返回所有与 [lo, hi) 相交的区间。 */
    public synchronized List<Interval> findOverlaps(long lo, long hi) {
        validateQuery(lo, hi);
        List<Interval> out = new ArrayList<>();
        collectOverlaps(root, lo, hi, out);
        return out;
    }

    /** 覆盖时刻 t（start &le; t &lt; end）的区间数量。 */
    public synchronized int countCovering(long t) {
        return countCovering(root, t);
    }

    /** 中序导出全部区间，供校验/调试。 */
    public synchronized List<Interval> toList() {
        List<Interval> out = new ArrayList<>(root.size);
        inorder(root, out);
        return out;
    }

    /**
     * 包内可见的结构自检（测试用）：校验 BST 序、红黑性质以及
     * size / maxEnd 聚合值；违反时抛出 {@link AssertionError}。
     *
     * @return 根的黑高
     */
    synchronized int checkInvariants() {
        if (root == nil) {
            return 1;
        }
        if (root.color != BLACK) {
            throw new AssertionError("root must be black");
        }
        return checkNode(root, null, null);
    }

    // ------------------------------------------------------------------
    // 查询辅助
    // ------------------------------------------------------------------

    private void collectOverlaps(Node node, long lo, long hi, List<Interval> out) {
        if (node == nil || node.maxEnd <= lo) {
            return;
        }
        if (node.left != nil && node.left.maxEnd > lo) {
            collectOverlaps(node.left, lo, hi, out);
        }
        if (node.start < hi && node.end > lo) {
            out.add(new Interval(node.id, node.start, node.end));
        }
        // 右子树所有起点 >= node.start；node.start >= hi 则右子树必无交集
        if (node.right != nil && node.start < hi && node.right.maxEnd > lo) {
            collectOverlaps(node.right, lo, hi, out);
        }
    }

    private int countCovering(Node node, long t) {
        if (node == nil || node.maxEnd <= t) {
            return 0;
        }
        if (t < node.start) {
            // 本节点与右子树起点都 > t，只有左子树可能覆盖
            return countCovering(node.left, t);
        }
        int c = countCovering(node.left, t);
        if (node.end > t) {
            c++;
        }
        c += countCovering(node.right, t);
        return c;
    }

    private void inorder(Node node, List<Interval> out) {
        if (node == nil) {
            return;
        }
        inorder(node.left, out);
        out.add(new Interval(node.id, node.start, node.end));
        inorder(node.right, out);
    }

    private static void validateQuery(long lo, long hi) {
        if (lo >= hi) {
            throw new IllegalArgumentException(
                    "invalid query [" + lo + ", " + hi + "): require lo < hi");
        }
    }

    // ------------------------------------------------------------------
    // 红黑树结构维护（CLRS 第 13 章）
    // ------------------------------------------------------------------

    /** 三元组键序：(start, end, id)。 */
    private static boolean less(Node a, Node b) {
        if (a.start != b.start) {
            return a.start < b.start;
        }
        if (a.end != b.end) {
            return a.end < b.end;
        }
        return a.id < b.id;
    }

    private Node minimum(Node x) {
        while (x.left != nil) {
            x = x.left;
        }
        return x;
    }

    private void transplant(Node u, Node v) {
        if (u.parent == nil) {
            root = v;
        } else if (u == u.parent.left) {
            u.parent.left = v;
        } else {
            u.parent.right = v;
        }
        v.parent = u.parent;
    }

    private void leftRotate(Node x) {
        Node y = x.right;
        x.right = y.left;
        if (y.left != nil) {
            y.left.parent = x;
        }
        y.parent = x.parent;
        if (x.parent == nil) {
            root = y;
        } else if (x == x.parent.left) {
            x.parent.left = y;
        } else {
            x.parent.right = y;
        }
        y.left = x;
        x.parent = y;
        pull(x);
        pull(y);
    }

    private void rightRotate(Node x) {
        Node y = x.left;
        x.left = y.right;
        if (y.right != nil) {
            y.right.parent = x;
        }
        y.parent = x.parent;
        if (x.parent == nil) {
            root = y;
        } else if (x == x.parent.right) {
            x.parent.right = y;
        } else {
            x.parent.left = y;
        }
        y.right = x;
        x.parent = y;
        pull(x);
        pull(y);
    }

    private void insertFixup(Node z) {
        while (z.parent.color == RED) {
            if (z.parent == z.parent.parent.left) {
                Node y = z.parent.parent.right;
                if (y.color == RED) {
                    z.parent.color = BLACK;
                    y.color = BLACK;
                    z.parent.parent.color = RED;
                    z = z.parent.parent;
                } else {
                    if (z == z.parent.right) {
                        z = z.parent;
                        leftRotate(z);
                    }
                    z.parent.color = BLACK;
                    z.parent.parent.color = RED;
                    rightRotate(z.parent.parent);
                }
            } else {
                Node y = z.parent.parent.left;
                if (y.color == RED) {
                    z.parent.color = BLACK;
                    y.color = BLACK;
                    z.parent.parent.color = RED;
                    z = z.parent.parent;
                } else {
                    if (z == z.parent.left) {
                        z = z.parent;
                        rightRotate(z);
                    }
                    z.parent.color = BLACK;
                    z.parent.parent.color = RED;
                    leftRotate(z.parent.parent);
                }
            }
        }
        root.color = BLACK;
    }

    private void deleteFixup(Node x) {
        while (x != root && x.color == BLACK) {
            if (x == x.parent.left) {
                Node w = x.parent.right;
                if (w.color == RED) {
                    w.color = BLACK;
                    x.parent.color = RED;
                    leftRotate(x.parent);
                    w = x.parent.right;
                }
                if (w.left.color == BLACK && w.right.color == BLACK) {
                    w.color = RED;
                    x = x.parent;
                } else {
                    if (w.right.color == BLACK) {
                        w.left.color = BLACK;
                        w.color = RED;
                        rightRotate(w);
                        w = x.parent.right;
                    }
                    w.color = x.parent.color;
                    x.parent.color = BLACK;
                    w.right.color = BLACK;
                    leftRotate(x.parent);
                    x = root;
                }
            } else {
                Node w = x.parent.left;
                if (w.color == RED) {
                    w.color = BLACK;
                    x.parent.color = RED;
                    rightRotate(x.parent);
                    w = x.parent.left;
                }
                if (w.right.color == BLACK && w.left.color == BLACK) {
                    w.color = RED;
                    x = x.parent;
                } else {
                    if (w.left.color == BLACK) {
                        w.right.color = BLACK;
                        w.color = RED;
                        leftRotate(w);
                        w = x.parent.left;
                    }
                    w.color = x.parent.color;
                    x.parent.color = BLACK;
                    w.left.color = BLACK;
                    rightRotate(x.parent);
                    x = root;
                }
            }
        }
        x.color = BLACK;
    }

    // ------------------------------------------------------------------
    // 聚合信息维护
    // ------------------------------------------------------------------

    /**
     * 递归校验单个节点：BST 键序、红黑性质、size 与 maxEnd。
     *
     * @param min 键序下界（开），null 表示无下界
     * @param max 键序上界（开），null 表示无上界
     * @return 该节点的黑高
     */
    private int checkNode(Node n, Node min, Node max) {
        if (n == nil) {
            return 1;
        }
        if (min != null && !less(min, n)) {
            throw new AssertionError("BST order violated: lower bound");
        }
        if (max != null && !less(n, max)) {
            throw new AssertionError("BST order violated: upper bound");
        }
        if (n.left != nil && n.left.parent != n) {
            throw new AssertionError("left child parent pointer mismatch");
        }
        if (n.right != nil && n.right.parent != n) {
            throw new AssertionError("right child parent pointer mismatch");
        }
        if (n.color == RED && (n.left.color == RED || n.right.color == RED)) {
            throw new AssertionError("red node has red child (id=" + n.id + ")");
        }
        int lb = checkNode(n.left, min, n);
        int rb = checkNode(n.right, n, max);
        if (lb != rb) {
            throw new AssertionError("black height mismatch (id=" + n.id
                    + ") left=" + lb + " right=" + rb);
        }
        int expectedSize = 1 + n.left.size + n.right.size;
        if (n.size != expectedSize) {
            throw new AssertionError("size mismatch (id=" + n.id
                    + ") expected=" + expectedSize + " actual=" + n.size);
        }
        long expectedMax = Math.max(n.end, Math.max(n.left.maxEnd, n.right.maxEnd));
        if (n.maxEnd != expectedMax) {
            throw new AssertionError("maxEnd mismatch (id=" + n.id
                    + ") expected=" + expectedMax + " actual=" + n.maxEnd);
        }
        return lb + (n.color == BLACK ? 1 : 0);
    }

    /** 重算单个节点的 size / maxEnd（依赖左右孩子已正确）。 */
    private void pull(Node n) {
        n.size = 1 + n.left.size + n.right.size;
        long m = n.end;
        if (n.left.maxEnd > m) {
            m = n.left.maxEnd;
        }
        if (n.right.maxEnd > m) {
            m = n.right.maxEnd;
        }
        n.maxEnd = m;
    }

    /** 从 anchor 沿父链重算到根。 */
    private void pullChain(Node anchor) {
        Node n = anchor;
        while (n != nil && n != null) {
            pull(n);
            n = n.parent;
        }
    }
}
