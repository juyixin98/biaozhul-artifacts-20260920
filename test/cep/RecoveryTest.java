package cep;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * 验收 3（部分）：故障恢复后部分匹配状态一致。
 *
 * 做法：用预写日志记录若干批次，构造“匹配了一部分、还留着部分匹配”的时刻，
 * 把日志交给一个全新的引擎重放（模拟进程重启），逐字段比较：
 *   - 已完成匹配集合（数量与具体事件序号）
 *   - 各实体等待中的 A 与 A->B
 *   - 时间水位
 *   - 下一个输入序号（恢复后新事件的序号必须接续）
 *
 * 进程级（真的 halt 再重启 JVM）的验证在 RecoveryProcessTest 中完成。
 */
public class RecoveryTest {

    private Path tempDir() throws Exception {
        return java.nio.file.Files.createTempDirectory("cep-recovery-test-");
    }

    /** 构造“有已完成匹配 + 有未完成部分匹配”的混合状态并校验重放一致性。 */
    public void testPartialStateIdenticalAfterReplay() throws Exception {
        Path dir = tempDir();

        // 崩溃前的写入历史（故意跨多个批次）：
        //   实体 done: A(0) B(100) C(200)           -> 已完成 1 个匹配，部分状态已空
        //   实体 x:    A(10000) A(10100) B(10200)   -> 2 个 A->B 部分匹配在等待 C
        //   实体 y:    X(9000) A(9100) B(9200)      -> 1 个 A->B 部分匹配在等待 C，
        //                                              X 验证无关事件也进入日志
        try (EventLog log = new EventLog(dir)) {
            Engine before = new Engine();
            writeBatch(before, log,
                    new Event("A", "done", 0, -1),
                    new Event("B", "done", 100, -1),
                    new Event("C", "done", 200, -1));
            writeBatch(before, log,
                    new Event("A", "x", 10_000, -1),
                    new Event("A", "x", 10_100, -1));
            writeBatch(before, log,
                    new Event("B", "x", 10_200, -1));
            writeBatch(before, log,
                    new Event("X", "y", 9_000, -1),
                    new Event("A", "y", 9_100, -1),
                    new Event("B", "y", 9_200, -1));

            // 崩溃前快照
            Snapshot snapBefore = Snapshot.capture(before);

            // 模拟重启：新引擎，仅靠日志恢复
            Engine after = new Engine();
            after.recover(log.replay());
            Snapshot snapAfter = Snapshot.capture(after);

            TestRunner.checkEq(snapAfter.totalMatches, snapBefore.totalMatches,
                    "恢复后已完成匹配总数必须一致");
            TestRunner.check(snapAfter.matches.equals(snapBefore.matches),
                    "恢复后匹配明细（含输入序号）必须逐一致: "
                            + snapBefore.matches + " != " + snapAfter.matches);
            TestRunner.check(snapAfter.states.equals(snapBefore.states),
                    "恢复后部分匹配状态必须逐一致: "
                            + snapBefore.states + " != " + snapAfter.states);
            TestRunner.checkEq(after.nextSeq(), before.nextSeq(),
                    "恢复后输入序号水位必须一致");

            // 恢复后继续工作：给 x 补窗口内的 C，两个等待中的 A->B 同时完成，
            // 且新事件序号接续（崩溃前 3+2+1+3=9 个事件，序号 0..8，下一个为 9）。
            Engine.IngestResult cont = after.ingest(List.of(new Event("C", "x", 10_300, -1)));
            TestRunner.checkEq(cont.created.size(), 2,
                    "恢复后等待中的 2 个 A->B 应能被新 C 同时匹配");
            TestRunner.checkEq(cont.accepted.get(0).seq, 9L,
                    "新事件的输入序号必须接续崩溃前（0..8 已用，下一个为 9）");
        }
    }

    /** 没有日志时冷启动：空状态、序号从 0 开始。 */
    public void testColdStartIsEmpty() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            Engine engine = new Engine();
            engine.recover(log.replay());
            TestRunner.checkEq(engine.totalMatches(), 0, "冷启动无匹配");
            TestRunner.checkEq(engine.nextSeq(), 0, "冷启动序号从 0 开始");
            TestRunner.check(engine.viewAllStates().isEmpty(), "冷启动无实体状态");
        }
    }

    /** 重放决定论：同一日志重放两次，结果必须完全相同。 */
    public void testReplayIsDeterministic() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            Engine seed = new Engine();
            writeBatch(seed, log, new Event("A", "x", 0, -1));
            writeBatch(seed, log,
                    new Event("B", "x", 100, -1),
                    new Event("C", "x", 200, -1));

            Engine first = new Engine();
            first.recover(log.replay());
            Engine second = new Engine();
            second.recover(log.replay());
            TestRunner.check(Snapshot.capture(first).equals(Snapshot.capture(second)),
                    "同一日志任意次重放结果必须相同");
        }
    }

    /** 走与 HTTP 层完全相同的两阶段路径（预检 -> WAL 追加 fsync -> 提交）写一批。 */
    private static void writeBatch(Engine engine, EventLog log, Event... raws) {
        Engine.PreparedBatch prepared = engine.ingestDryRun(List.of(raws));
        log.appendBatch(prepared.assigned);
        engine.commit(prepared);
    }

    /** 引擎全状态的可比较快照（仅测试用）。 */
    static final class Snapshot {
        final long totalMatches;
        final List<String> matches;
        final List<String> states;

        private Snapshot(long totalMatches, List<String> matches, List<String> states) {
            this.totalMatches = totalMatches;
            this.matches = matches;
            this.states = states;
        }

        static Snapshot capture(Engine engine) {
            List<String> ms = new ArrayList<>();
            for (Match m : engine.queryMatches()) {
                ms.add(m.entityId() + ":" + m.a.seq + "->" + m.b.seq + "->" + m.c.seq
                        + "@" + (m.c.timestamp - m.a.timestamp));
            }
            List<String> st = new ArrayList<>();
            for (Engine.EntityStateView v : engine.viewAllStates()) {
                List<String> as = new ArrayList<>();
                for (Event a : v.waitingAs) {
                    as.add("A#" + a.seq + "(ts=" + a.timestamp + ")");
                }
                List<String> abs = new ArrayList<>();
                for (Engine.ABView ab : v.waitingABs) {
                    abs.add("A#" + ab.a.seq + "->B#" + ab.b.seq);
                }
                st.add(v.entityId + "|wm=" + v.watermark
                        + "|A=" + String.join(",", as)
                        + "|AB=" + String.join(",", abs));
            }
            return new Snapshot(engine.totalMatches(), ms, st);
        }

        @Override
        public boolean equals(Object o) {
            if (!(o instanceof Snapshot)) return false;
            Snapshot s = (Snapshot) o;
            return totalMatches == s.totalMatches
                    && matches.equals(s.matches)
                    && states.equals(s.states);
        }

        @Override
        public int hashCode() {
            return java.util.Objects.hash(totalMatches, matches, states);
        }
    }
}
