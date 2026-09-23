package dedup.tests;

import dedup.core.BoundedTombstoneDeduplicator;
import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.json.Json;

/** 去重算子边界单测：墓碑锚点随窗口内更晚副本延长，且不被更早副本影响。 */
final class DeduplicatorEdgeTest {

    private DeduplicatorEdgeTest() {}

    static void register(TestFramework tf) {
        tf.run("墓碑锚点：更早副本不提前释放，更晚副本延长寿命，释放后再无承诺", a -> {
            var d = new BoundedTombstoneDeduplicator(5_000, 1000);
            // 首次 eventTime=20000
            d.process(ev("z", 20_000));
            // 副本带着更早的 eventTime=18000 到达（乱序），在窗口内被抑制，锚点保持 20000
            a.eq(d.process(ev("z", 18_000)).decision(), DedupResult.Decision.SUPPRESS,
                    "更早 eventTime 的副本应抑制，锚点不变");

            // 水位线 24000：下界 19000，锚点 20000 仍在窗口内
            d.onWatermark(24_000);
            a.eqLong(d.activeTombstones(), 1, "墓碑保留");

            // 更晚的乱序副本 eventTime=23000 在当前窗口内被抑制，锚点延长到 23000
            a.eq(d.process(ev("z", 23_000)).decision(), DedupResult.Decision.SUPPRESS,
                    "更晚副本抑制并延长锚点");

            // 若锚点没有延长到 23000，水位线 26000（下界 21000）就会释放；延长后必须仍在
            d.onWatermark(26_000);
            a.eqLong(d.activeTombstones(), 1,
                    "锚点延长后，旧水位线不能释放墓碑");
            a.eq(d.process(ev("z", 23_000)).decision(), DedupResult.Decision.SUPPRESS,
                    "延长期间重复继续抑制");

            // 水位线 28001：下界 23001，锚点 23000 越界，此时释放
            d.onWatermark(28_001);
            a.eqLong(d.activeTombstones(), 0, "越界后墓碑释放");
            a.eq(d.process(ev("z", 23_000)).decision(), DedupResult.Decision.UNGUARANTEED,
                    "释放后透传并标记不可保证");
        });

        tf.run("构造参数校验", a -> {
            a.throws_(IllegalArgumentException.class,
                    () -> new BoundedTombstoneDeduplicator(-1, 10), "负的 allowedLateness");
            a.throws_(IllegalArgumentException.class,
                    () -> new BoundedTombstoneDeduplicator(0, 0), "maxTombstones=0");
            a.throws_(IllegalArgumentException.class,
                    () -> new Event(null, "", 1, Json.Nul.INSTANCE), "空事件ID");
        });

        tf.run("极小硬上限(maxTombstones=1)也能稳定工作且不越界", a -> {
            var d = new BoundedTombstoneDeduplicator(1_000_000, 1);
            d.process(ev("a", 1));
            a.eqLong(d.activeTombstones(), 1, "第一条墓碑存在");
            d.process(ev("b", 2)); // 触发降到 target=0
            a.check(d.activeTombstones() <= 1, "状态数始终 <= 上限, 实际=" + d.activeTombstones());
            // a 的墓碑一定被淘汰
            a.eq(d.process(ev("a", 1)).decision(), DedupResult.Decision.UNGUARANTEED,
                    "被淘汰的 a 标记不可保证");
            // 新键仍可正常接受（淘汰腾出空间后不会卡死）
            a.eq(d.process(ev("c", 3)).decision(), DedupResult.Decision.ACCEPT, "新键 c 可接受");
        });
    }

    private static Event ev(String id, long t) {
        return new Event(null, id, t, Json.Nul.INSTANCE);
    }
}
