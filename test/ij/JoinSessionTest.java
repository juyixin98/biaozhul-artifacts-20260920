package ij;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/** JoinSession 引擎核心语义测试：闭区间边界、迟到、双方水位回收、热键、去重。 */
final class JoinSessionTest {

    private final Assert a;

    JoinSessionTest(Assert a) {
        this.a = a;
        basicAndOrder();
        inclusiveBounds();
        lateDropped();
        evictionNeedsBothWatermarks();
        negativeBounds();
        idDedup();
        multiKeyEviction();
    }

    private JoinSession push(JoinSession s, String side, String key, long ts) {
        s.pushEvent(side, key, ts, null, null);
        return s;
    }

    /** 规范化配对集合，方便与期望集合比较。 */
    private TreeSet<String> signatures(JoinSession s) {
        TreeSet<String> out = new TreeSet<>();
        for (Pair p : s.pairs()) {
            out.add(p.left.key + ":" + p.left.ts + "=" + p.right.ts);
        }
        return out;
    }

    private TreeSet<String> sig(String... items) {
        TreeSet<String> s = new TreeSet<>();
        for (String i : items) {
            s.add(i);
        }
        return s;
    }

    private long stat(JoinSession s, String k) {
        Object v = s.snapshotStats().get(k);
        return ((Number) v).longValue();
    }

    private void basicAndOrder() {
        // 区间 [-2, +2]：L@5 与 R@3,4,5,6,7 配对。
        JoinSession s = new JoinSession("t", -2, 2);
        push(s, "left", "u", 5);
        push(s, "right", "u", 3);
        push(s, "right", "u", 4);
        push(s, "right", "u", 6);
        push(s, "right", "u", 7);
        push(s, "right", "u", 8); // 超出上界
        push(s, "right", "v", 5); // 不同 key
        a.eq(signatures(s), sig("u:5=3", "u:5=4", "u:5=6", "u:5=7"),
                "left-first order pairs");

        // 反向到达顺序同样正确（右事件先到）。
        JoinSession s2 = new JoinSession("t", -2, 2);
        push(s2, "right", "u", 3);
        push(s2, "right", "u", 8);
        push(s2, "left", "u", 5);
        a.eq(signatures(s2), sig("u:5=3"), "right-first order pairs");
    }

    private void inclusiveBounds() {
        JoinSession s = new JoinSession("t", 0, 0);
        push(s, "left", "u", 10);
        push(s, "right", "u", 10); // r.ts == l.ts，闭区间命中
        push(s, "right", "u", 11);
        a.eq(s.pairs().size(), 1L, "[0,0] joins only same timestamp");

        JoinSession s3 = new JoinSession("t", 1, 3);
        push(s3, "left", "u", 10);
        push(s3, "right", "u", 11); // == l+1 下界，命中
        push(s3, "right", "u", 13); // == l+3 上界，命中
        push(s3, "right", "u", 14);
        a.eq(s3.pairs().size(), 2L, "both bounds inclusive");
    }

    private void lateDropped() {
        JoinSession s = new JoinSession("t", -2, 2);
        push(s, "left", "u", 10);
        s.advanceWatermark("left", 8);
        // eventTime 8 <= wm 8：迟到
        EventResult late = s.pushEvent("left", "u", 8L, null, null);
        a.check(late.dropped, "ts == watermark is late");
        a.eq(stat(s, "leftDroppedLate"), 1L, "left late counter");
        a.eq(stat(s, "leftStateEvents"), 1L, "late event not stored");

        // 迟到右事件不产生配对。
        push(s, "right", "u", 10);
        s.advanceWatermark("right", 10);
        EventResult lateR = s.pushEvent("right", "u", 10L, null, null);
        a.check(lateR.dropped, "right late event dropped");
        a.eq(s.pairs().size(), 1L, "late right event creates no pair");
    }

    private void evictionNeedsBothWatermarks() {
        JoinSession s = new JoinSession("t", -2, 2);
        push(s, "left", "u", 10);
        push(s, "right", "u", 10);

        // 仅左水位推进：右状态不能回收（左侧未来仍可能有更晚的左事件与其配对），
        // 左状态本身也不能回收（右水位未证明上界之外）。
        s.advanceWatermark("left", 100);
        a.eq(stat(s, "leftStateEvents"), 1L, "left kept: opposite watermark behind");
        a.eq(stat(s, "rightStateEvents"), 1L, "right kept: own watermark behind");
        a.eq(stat(s, "totalReclaimed"), 0L, "nothing reclaimed with one watermark");

        // 右水位跟上：cutL = min(100, 100-2)=98 ⇒ 左10 回收；cutR = min(100, 100+2)=100 ⇒ 右10 回收。
        int n = s.advanceWatermark("right", 100);
        a.eq(n, 2L, "both events reclaimed when both watermarks advance");
        a.eq(stat(s, "leftStateEvents"), 0L, "left state empty after reclaim");
        a.eq(stat(s, "rightStateEvents"), 0L, "right state empty after reclaim");
        a.eq(stat(s, "leftStateKeys"), 0L, "empty key buckets removed");

        // 回收不影响已输出配对。
        a.eq(s.pairs().size(), 1L, "pairs survive state reclaim");

        // 边界临界：wr == l.ts + upperBound ⇒ 可回收；差 1 时不可回收。
        JoinSession s2 = new JoinSession("t", 0, 5);
        push(s2, "left", "u", 10);
        push(s2, "right", "u", 20); // 不配对
        s2.advanceWatermark("left", 20);
        s2.advanceWatermark("right", 14); // 14 < 10+5
        a.eq(stat(s2, "leftStateEvents"), 1L, "left kept at wr = upper-1");
        a.eq(stat(s2, "rightStateEvents"), 1L, "right kept: wr < r.ts");
        s2.advanceWatermark("right", 15);
        a.eq(stat(s2, "leftStateEvents"), 0L, "left reclaimed at wr == l.ts+upper");
    }

    private void negativeBounds() {
        // 区间 [-5, -1]：r.ts 在 l.ts-5..l.ts-1。
        JoinSession s = new JoinSession("t", -5, -1);
        push(s, "left", "u", 10);
        push(s, "right", "u", 5);   // 命中下界
        push(s, "right", "u", 9);   // 命中上界
        push(s, "right", "u", 10);  // 超出
        a.eq(s.pairs().size(), 2L, "negative interval joins earlier rights");

        s.advanceWatermark("left", 100);
        s.advanceWatermark("right", 100);
        a.eq(stat(s, "totalReclaimed"), 4L, "reclaim all with negative bounds (1 left + 3 right)");
    }

    private void idDedup() {
        JoinSession s = new JoinSession("t", -10, 10);
        s.pushEvent("left", "u", 1L, "dup", null);
        s.pushEvent("right", "u", 1L, "r1", null);
        a.eq(s.pairs().size(), 1L, "first pair emitted");
        // 复用相同 id 的事件（语义上是同一事件重复接入）不重复产生配对。
        s.pushEvent("left", "u", 1L, "dup", null);
        a.eq(s.pairs().size(), 1L, "same id pair not emitted twice");
    }

    private void multiKeyEviction() {
        JoinSession s = new JoinSession("t", 0, 10);
        push(s, "left", "a", 1);
        push(s, "left", "b", 100);
        push(s, "right", "c", 2);
        s.advanceWatermark("left", 50);
        s.advanceWatermark("right", 50);
        // cutL = min(50, 40)=40：a@1 回收，b@100 保留；cutR = min(50,50)=50：c@2 回收。
        a.eq(stat(s, "leftReclaimed"), 1L, "only expired key's left event reclaimed");
        a.eq(stat(s, "rightReclaimed"), 1L, "right event reclaimed");
        a.eq(stat(s, "leftStateEvents"), 1L, "hot/far key retained");
    }
}
