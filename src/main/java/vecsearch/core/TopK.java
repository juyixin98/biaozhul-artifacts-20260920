package vecsearch.core;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * 流式维护最小的 k 个 (distance, id)。
 *
 * <p>内部是一个大小不超过 k 的最大堆：堆顶是当前第 k 近（最远）的候选，
 * 新候选只有严格更近才替换。等距时按 ID 字典序排序，保证结果稳定可复现。
 */
public final class TopK {

    /** 堆顶 = 当前最差（距离最大；距离相同则 id 字典序最大）的元素。 */
    public record Node(float distance, String id) {
    }

    private static final Comparator<Node> WORST_FIRST =
            Comparator.comparingDouble((Node n) -> n.distance)
                    .thenComparing(n -> n.id)
                    .reversed();

    private final int k;
    private final PriorityQueue<Node> heap;

    public TopK(int k) {
        if (k <= 0) {
            throw new IllegalArgumentException("k must be positive, got " + k);
        }
        this.k = k;
        this.heap = new PriorityQueue<>(WORST_FIRST);
    }

    /** 尝试纳入一个候选。 */
    public void offer(float distance, String id) {
        if (heap.size() < k) {
            heap.offer(new Node(distance, id));
        } else {
            Node worst = heap.peek();
            if (better(distance, id, worst.distance(), worst.id())) {
                heap.poll();
                heap.offer(new Node(distance, id));
            }
        }
    }

    private static boolean better(float d1, String id1, float d2, String id2) {
        if (d1 != d2) {
            return d1 < d2;
        }
        return id1.compareTo(id2) < 0;
    }

    public int size() {
        return heap.size();
    }

    /** 当前最差候选的距离（用于提前终止）；不足 k 个时返回 +inf。 */
    public float worstDistance() {
        return heap.isEmpty() ? Float.POSITIVE_INFINITY : heap.peek().distance();
    }

    /** 取出结果，按距离升序（等距按 id 升序）。 */
    public List<Node> drain() {
        List<Node> all = new ArrayList<>(heap);
        all.sort(Comparator.comparingDouble((Node n) -> n.distance).thenComparing(Node::id));
        heap.clear();
        return all;
    }
}
