package dedup.tests;

import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.json.Json;
import dedup.state.InMemorySnapshotStore;
import dedup.state.SnapshotStore;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 验收场景 3：重启恢复。
 *
 * <p>进程“重启”= 丢弃 StreamProcessor 实例，用同一个 {@link SnapshotStore} 重建。
 * 恢复后：墓碑、水位线、强制淘汰下界、计数器全部延续；
 * 已在窗口内见过的 ID 重启后再来副本仍然被抑制（无重复输出）。
 * 覆盖内存存储与真实文件存储（含临时文件不产生半截快照）。
 */
final class RestartRecoveryTest {

    private RestartRecoveryTest() {}

    static void register(TestFramework tf) {
        tf.run("重启恢复：内存快照重建后墓碑/水位线/计数器延续，窗口内重复继续抑制", a -> {
            var store = new InMemorySnapshotStore();
            var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);

            var before = StreamProcessor.create(config, Clock.manual(100), store);
            before.process(ev("e1", 10_000, "{\"n\":1}"));
            before.process(ev("e2", 10_000, "{\"n\":2}"));
            before.process(ev("e1", 10_000, "{\"n\":1}")); // 重复，抑制
            before.setManualWatermark(12_000);             // 推进并落盘
            // 再制造一条极迟事件，让计数器带上 unguarded
            before.process(ev("late", 1_000, "{}"));       // < 12000-5000=7000
            before.checkpoint();

            long acceptedBefore = stat(before, "accepted");
            long suppressedBefore = stat(before, "suppressed");
            long unguardedBefore = stat(before, "unguarded");
            long activeBefore = stat(before, "activeTombstones");

            // === 模拟重启：旧实例丢弃，新实例从同一存储创建 ===
            var after = StreamProcessor.create(config, Clock.manual(200), store);

            a.eqLong(stat(after, "activeTombstones"), activeBefore,
                    "墓碑数量恢复一致");
            a.eqLong(stat(after, "accepted"), acceptedBefore, "accepted 计数恢复");
            a.eqLong(stat(after, "suppressed"), suppressedBefore, "suppressed 计数恢复");
            a.eqLong(stat(after, "unguarded"), unguardedBefore, "unguarded 计数恢复");
            a.eqLong(after.deduplicator().currentWatermark(), 12_000,
                    "水位线恢复为 12000");

            // 重启后来窗口内副本 —— 必须继续抑制，无重复输出
            DedupResult dupAfterRestart = after.process(ev("e2", 10_000, "{\"n\":2}"));
            a.eq(dupAfterRestart.decision(), DedupResult.Decision.SUPPRESS,
                    "重启后窗口内已知 ID 副本必须抑制");

            // 重启后新事件正常接受，计数器在恢复基础上递增
            a.eq(after.process(ev("e3", 10_000, "{\"n\":3}")).decision(),
                    DedupResult.Decision.ACCEPT, "新事件正常接受");
            a.eqLong(stat(after, "suppressed"), suppressedBefore + 1,
                    "抑制计数在历史值上递增");
        });

        tf.run("重启恢复：文件快照原子落盘，无snapshot.tmp残留，内容可解析", a -> {
            Path dir = Files.createTempDirectory("dedup-restart-");
            Path file = dir.resolve("snapshot.json");
            try {
                var store1 = new dedup.state.FileSnapshotStore(file);
                var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);
                var p1 = StreamProcessor.create(config, Clock.manual(0), store1);
                p1.process(ev("f1", 10_000, "{\"x\":1}"));
                p1.process(ev("f2", 11_000, "{\"x\":2}"));
                p1.setManualWatermark(13_000);

                a.check(Files.exists(file), "snapshot.json 必须存在");
                a.check(!Files.exists(dir.resolve("snapshot.json.tmp")),
                        "原子 move 后不得残留 .tmp 文件");

                String raw = Files.readString(file);
                Json.Value parsed = Json.parse(raw);
                a.check(parsed instanceof Json.Obj, "快照文件必须是合法 JSON 对象");

                // 全新进程语义：再建一个处理器，基于文件恢复
                var store2 = new dedup.state.FileSnapshotStore(file);
                var p2 = StreamProcessor.create(config, Clock.manual(0), store2);
                a.eq(p2.process(ev("f1", 10_000, "{\"x\":1}")).decision(),
                        DedupResult.Decision.SUPPRESS, "文件恢复后 f1 副本仍被抑制");
            } finally {
                Files.walk(dir)
                        .sorted((x, y) -> y.compareTo(x))
                        .forEach(p -> { try { Files.deleteIfExists(p); } catch (Exception ignored) {} });
            }
        });

        tf.run("重启恢复：bounded模式内部观测最大值恢复，水位线不回退", a -> {
            var store = new InMemorySnapshotStore();
            var config = new StreamProcessor.Config(5_000, 1000, "bounded", 2_000, 1000);
            var p1 = StreamProcessor.create(config, Clock.manual(0), store);
            p1.process(ev("b1", 20_000, "{}"));
            p1.tick(); // 水位线 18000
            p1.checkpoint();

            var p2 = StreamProcessor.create(config, Clock.manual(1_000), store);
            a.eqLong(p2.deduplicator().currentWatermark(), 18_000, "水位线恢复");
            // 重启后 tick，没有更大事件时间时水位线保持
            a.check(p2.tick() == null, "无更大观测值时 tick 不应推进水位线");
            // 旧事件时间 19000 不能把内部最大值拉低，后续 tick 仍不回退
            p2.process(ev("b2", 19_000, "{}"));
            a.check(p2.tick() == null, "旧事件不应导致水位线变化");
        });
    }

    private static Event ev(String id, long t, String payloadJson) {
        return new Event(null, id, t, Json.parse(payloadJson));
    }

    private static long stat(StreamProcessor p, String key) {
        return ((Json.Num) p.statsJson().get(key)).value().longValueExact();
    }
}
