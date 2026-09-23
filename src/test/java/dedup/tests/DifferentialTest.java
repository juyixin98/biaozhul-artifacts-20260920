package dedup.tests;

import dedup.core.BoundedTombstoneDeduplicator;
import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.core.ReferenceDeduplicator;
import dedup.json.Json;

import java.util.Random;

/**
 * 差分测试（小数据精确参考实现 vs 生产有界算子）。
 *
 * <p>随机生成乱序事件流（ID 取自小集合以制造重复，eventTime 围绕基线抖动以制造乱序/迟到），
 * 两个实现用<b>相同的水位线序列</b>驱动；当有界算子的墓碑数未触及硬上限时，
 * 二者在承诺范围内对每个事件的 {@code decision} 必须逐条相同。
 *
 * <p>这是“承诺范围内无重复输出”的高强度核对：参考实现永不淘汰墓碑，
 * 任何分歧都意味着有界实现错误地抑制/透放了事件。
 */
final class DifferentialTest {

    private DifferentialTest() {}

    static void register(TestFramework tf) {
        tf.run("差分测试：100条随机乱序流，有界算子在未触顶时与无界参考实现判定逐条一致", a -> {
            long seed = 20260923L;
            int streams = 300;
            int mismatches = 0;
            long totalEvents = 0;
            long totalGuaranteed = 0;

            for (int s = 0; s < streams; s++) {
                Random rnd = new Random(seed + s);
                long lateness = 1 + rnd.nextInt(50);
                int idSpace = 2 + rnd.nextInt(8);
                int eventsPerStream = 30 + rnd.nextInt(70);
                int maxTombstones = 10_000; // 远大于 idSpace，确保不触顶

                var bounded = new BoundedTombstoneDeduplicator(lateness, maxTombstones);
                var ref = new ReferenceDeduplicator(lateness);

                long base = 1_000;
                for (int i = 0; i < eventsPerStream; i++) {
                    String id = "id-" + rnd.nextInt(idSpace);
                    // 70% 事件在近期窗口，30% 制造迟到（可能越过承诺窗口）
                    long t = (rnd.nextInt(10) < 7)
                            ? base + rnd.nextInt(120)
                            : base - rnd.nextInt((int) (lateness * 3L) + 60);
                    if (t < 0) t = 0;
                    Json.Value payload = rnd.nextInt(5) == 0
                            ? Json.parse("{\"v\":" + rnd.nextInt(3) + "}")
                            : Json.Nul.INSTANCE;
                    Event e = new Event(null, id, t, payload);

                    DedupResult rb = bounded.process(e);
                    DedupResult rr = ref.process(e);
                    totalEvents++;
                    if (rb.dedupGuaranteed()) totalGuaranteed++;

                    if (rb.decision() != rr.decision()) {
                        mismatches++;
                        if (mismatches <= 5) {
                            a.fail(String.format(
                                    "流 %d 事件 %d (id=%s,t=%d): 有界=%s 参考=%s",
                                    s, i, id, t, rb.decision(), rr.decision()));
                        }
                    }
                    // 承诺范围内，载荷不一致标记也必须一致
                    if (rb.decision() == DedupResult.Decision.SUPPRESS
                            && rb.payloadMismatch() != rr.payloadMismatch()) {
                        mismatches++;
                        if (mismatches <= 5) {
                            a.fail(String.format(
                                    "流 %d 事件 %d (id=%s,t=%d): payloadMismatch 有界=%s 参考=%s",
                                    s, i, id, t, rb.payloadMismatch(), rr.payloadMismatch()));
                        }
                    }

                    // 两个实现用同一水位线序列：随事件流推进
                    if (i % 5 == 4) {
                        long wm = base + rnd.nextInt(80);
                        bounded.onWatermark(wm);
                        ref.onWatermark(wm);
                    }
                }
            }
            a.eqLong(mismatches, 0, "差分分歧总数必须为 0");
            System.out.println("        (核对 " + totalEvents + " 个事件，其中承诺范围内 "
                    + totalGuaranteed + " 个)");
        });

        tf.run("不变量：同一承诺窗口[horizon,+∞)内，已接受ID的后续副本必被抑制；SUPPRESS必在承诺内", a -> {
            // 精确契约（逐条事件独立可验证）：
            //  (1) 凡是 SUPPRESS，dedupGuaranteed 必须为 true；
            //  (2) 对每个事件，只要它的 (key,id) 在“当前窗口内已被接受过”，它就必须被抑制；
            //  (3) UNGUARANTEED 透传事件 dedupGuaranteed 必须为 false。
            // 窗口随水位线移动，墓碑一旦因越窗释放，更老的副本再来不属于承诺范围——这是设计边界。
            Random rnd = new Random(42L);
            int violations = 0;
            int suppressNotGuaranteed = 0;
            int unguardedButFlagged = 0;
            int checkedWindows = 0;

            for (int s = 0; s < 200; s++) {
                long lateness = 1 + rnd.nextInt(30);
                var dedup = new BoundedTombstoneDeduplicator(lateness, 10_000);
                // 镜像生产算子的存活墓碑：id -> 墓碑锚点 eventTime
                java.util.Map<String, Long> liveTomb = new java.util.HashMap<>();
                Long wm = null;
                for (int i = 0; i < 300; i++) {
                    String id = "k" + rnd.nextInt(12);
                    long t = rnd.nextInt(400);
                    long horizon = wm == null ? Long.MIN_VALUE : wm - lateness;
                    boolean inWindow = wm == null || t >= horizon;
                    boolean tombPresent =
                            inWindow && liveTomb.containsKey(id) && liveTomb.get(id) >= horizon;

                    var r = dedup.process(new Event(null, id, t, Json.Nul.INSTANCE));

                    if (r.decision() == DedupResult.Decision.SUPPRESS && !r.dedupGuaranteed()) {
                        suppressNotGuaranteed++;
                    }
                    if (r.decision() == DedupResult.Decision.UNGUARANTEED && r.dedupGuaranteed()) {
                        unguardedButFlagged++;
                    }
                    if (tombPresent) {
                        checkedWindows++;
                        if (r.decision() != DedupResult.Decision.SUPPRESS) {
                            violations++;
                            if (violations <= 5) {
                                a.fail(String.format(
                                        "s=%d i=%d id=%s t=%d wm=%s horizon=%d: 窗口内已有存活墓碑，却得到 %s",
                                        s, i, id, t, wm, horizon, r.decision()));
                            }
                        }
                    }
                    // 镜像状态变更：与生产算子完全对应
                    if (r.decision() == DedupResult.Decision.ACCEPT) {
                        liveTomb.put(id, t); // 首次接受写墓碑
                    } else if (r.decision() == DedupResult.Decision.SUPPRESS && t > liveTomb.get(id)) {
                        liveTomb.put(id, t); // 窗口内更晚副本延长锚点
                    }
                    if (i % 7 == 6) {
                        long newWm = (wm == null ? 0 : wm) + 8 + rnd.nextInt(12);
                        dedup.onWatermark(newWm);
                        wm = newWm;
                        long h = wm - lateness;
                        liveTomb.values().removeIf(anchor -> anchor < h); // 水位线释放
                    }
                }
            }
            a.eqLong(violations, 0, "窗口内漏抑制违例必须为 0（核对 " + checkedWindows + " 次窗口内重复）");
            a.eqLong(suppressNotGuaranteed, 0, "SUPPRESS 必须全部处于承诺范围内");
            a.eqLong(unguardedButFlagged, 0, "UNGUARANTEED 不得被错误标记为承诺");
        });
    }
}
