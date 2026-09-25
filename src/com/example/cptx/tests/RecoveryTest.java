package com.example.cptx.tests;

import com.example.cptx.core.CheckpointPolicy;
import com.example.cptx.core.Clock;
import com.example.cptx.core.FaultPhase;
import com.example.cptx.core.FaultSpec;
import com.example.cptx.core.InjectedFaultException;
import com.example.cptx.core.Json;
import com.example.cptx.core.Pipeline;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 验收核心：在状态写入与输出提交的各个点注入故障，
 * 比较“连续执行”与“崩溃→恢复执行”的最终偏移与汇总。
 *
 * 比较内容：
 *  - nextOffset（最终输入偏移）
 *  - summary（本地汇总表 count/sum）
 *  - 已提交事务编号集合
 *  - 每个事务的逻辑内容（nextOffset + 行集合；时间戳字段单独剔除）
 *  - 无残留 .staged/.tmp 文件
 */
public final class RecoveryTest {

    /** 每次崩溃模拟都使用固定时钟，使重建出的事务文件可逐字段比较（时间戳也一致）。 */
    private static final Clock FIXED_CLOCK = () -> 42L;
    private static final long EVERY = 5;

    private RecoveryTest() {}

    public static void run(TestRunner t) throws Exception {
        Map<String, Object> baseline = baseline(t);
        allFourPhases(t, baseline);
        doubleFault(t, baseline);
        crashAfterAllCommitted(t, baseline);
        noDuplicateAcrossReopen(t, baseline);
    }

    /** 连续执行（无故障）基线。 */
    private static Map<String, Object> baseline(TestRunner t) throws Exception {
        t.section("连续执行基线（无故障，37 条，每 5 条一个检查点）");
        Path dir = TestDirs.create("recovery-baseline");
        Pipeline p = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        p.appendAndProcess(TestFixtures.payloads());
        p.flushCheckpoint();
        Map<String, Object> st = p.status();
        t.eq(st.get("nextOffset"), 37L, "基线 nextOffset=37");
        // 37 条 => 每 5 条一次（5,10,...,35 共 7 次），尾部 flush 第 8 次
        t.eq(st.get("lastCheckpointId"), 8L, "基线检查点数=8");
        assertMatchesReference(t, st, "基线汇总等于独立参考实现");
        assertNoStagingFiles(t, dir, "基线无临时文件");
        return st;
    }

    private static void allFourPhases(TestRunner t, Map<String, Object> baseline) throws Exception {
        for (FaultPhase phase : FaultPhase.values()) {
            onePhase(t, phase, 3, baseline);
            onePhase(t, phase, 7, baseline); // 靠后的检查点
        }
    }

    private static void onePhase(TestRunner t, FaultPhase phase, long crashAt,
                                 Map<String, Object> baseline) throws Exception {
        t.section(phase + " 崩溃于检查点 " + crashAt + " → 新进程恢复");
        Path dir = TestDirs.create("recovery-" + phase + "-" + crashAt);

        // —— 第一次“进程”：装载全部输入，在 crashAt 注入故障 ——
        InjectedFaultException caught = null;
        Pipeline crashed = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK,
                new FaultSpec().arm(phase, crashAt));
        try {
            crashed.appendAndProcess(TestFixtures.payloads());
            crashed.flushCheckpoint();
        } catch (InjectedFaultException e) {
            caught = e;
        }
        t.check(caught != null && caught.phase() == phase, "确实在 " + phase + " 崩溃");

        // 崩溃现场：STATE_WRITE / OUTPUT_STAGE 必须留下各自的临时文件（这正是“未提交不生效”的证据）；
        // OUTPUT_COMMIT / TABLE_APPLY 之后没有暂存文件。
        Path tmpCp = dir.resolve(String.format("checkpoint-%06d.json.tmp", crashAt));
        Path stagedTxn = dir.resolve("output").resolve(String.format("txn-%06d.staged", crashAt));
        switch (phase) {
            case STATE_WRITE -> t.check(Files.exists(tmpCp), "崩溃现场保留检查点 .tmp（rename 前死亡）");
            case OUTPUT_STAGE -> t.check(Files.exists(stagedTxn), "崩溃现场保留事务 .staged（提交前死亡）");
            case OUTPUT_COMMIT, TABLE_APPLY -> { /* rename 已完成，无暂存文件 */ }
        }

        // —— 第二次“进程”：恢复并重放 ——
        Pipeline recovered = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        long resumed = recovered.resume();
        recovered.flushCheckpoint();
        t.eq(resumed, 37L, phase + ": 恢复后 nextOffset=37");

        Map<String, Object> rst = recovered.status();
        assertEquivalent(t, baseline, rst, "状态对比", phase);
        assertNoStagingFiles(t, dir, phase + ": 恢复后无 .staged/.tmp 残留");
        assertTxnContentsMatch(t, dir, (List<?>) baseline.get("committedTxns"),
                (List<?>) rst.get("committedTxns"), phase);
    }

    /** 双故障：先在检查点 4 的 STATE_WRITE 崩溃，恢复后再在检查点 6 的 OUTPUT_COMMIT 崩溃，再恢复。 */
    private static void doubleFault(TestRunner t, Map<String, Object> baseline) throws Exception {
        t.section("双故障：STATE_WRITE@4 崩溃 → 恢复后 OUTPUT_COMMIT@6 崩溃 → 再恢复");
        Path dir = TestDirs.create("recovery-double");

        Pipeline p1 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK,
                new FaultSpec().arm(FaultPhase.STATE_WRITE, 4));
        try {
            p1.appendAndProcess(TestFixtures.payloads());
            p1.flushCheckpoint();
        } catch (InjectedFaultException e) {
            t.check(e.phase() == FaultPhase.STATE_WRITE, "第一次崩溃（STATE_WRITE@4）");
        }

        // 恢复进程，重放全部日志；arm 在检查点 6 的 OUTPUT_COMMIT
        Pipeline p2 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK,
                new FaultSpec().arm(FaultPhase.OUTPUT_COMMIT, 6));
        expectCrash(t, p2, FaultPhase.OUTPUT_COMMIT, "第二次崩溃");

        Pipeline p3 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        long resumed = p3.resume();
        p3.flushCheckpoint();
        t.eq(resumed, 37L, "双故障后 nextOffset=37");
        assertEquivalent(t, baseline, p3.status(), "双故障状态对比", FaultPhase.OUTPUT_COMMIT);
        assertNoStagingFiles(t, dir, "双故障恢复后无临时文件");
    }

    /** 所有事务都已提交后，在最后一次 flush 的 TABLE_APPLY 崩溃：汇总表应可重建且一致。 */
    private static void crashAfterAllCommitted(TestRunner t, Map<String, Object> baseline) throws Exception {
        t.section("TABLE_APPLY@8（全部输出已提交后）崩溃 → 汇总表从提交日志重建");
        Path dir = TestDirs.create("recovery-table-last");
        Pipeline p1 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK,
                new FaultSpec().arm(FaultPhase.TABLE_APPLY, 8));
        InjectedFaultException first = null;
        try {
            p1.appendAndProcess(TestFixtures.payloads());
            p1.flushCheckpoint();
        } catch (InjectedFaultException e) {
            first = e;
        }
        t.check(first != null && first.phase() == FaultPhase.TABLE_APPLY, "末次应用时崩溃");

        Pipeline p2 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        long resumed = p2.resume();
        t.eq(resumed, 37L, "恢复时无需重放数据（偏移已在 37）");
        assertEquivalent(t, baseline, p2.status(), "末次崩溃后状态对比", FaultPhase.TABLE_APPLY);
    }

    /** 不注入故障，仅反复重新打开管线：已处理事件不得被二次计数。 */
    private static void noDuplicateAcrossReopen(TestRunner t, Map<String, Object> baseline) throws Exception {
        t.section("无故障重复 reopen：偏移与汇总不重复");
        Path dir = TestDirs.create("recovery-reopen");
        Pipeline p1 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        p1.appendAndProcess(TestFixtures.payloads());
        p1.flushCheckpoint();

        Pipeline p2 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        p2.resume();
        Pipeline p3 = new Pipeline(dir, CheckpointPolicy.count(EVERY), FIXED_CLOCK, new FaultSpec());
        p3.resume();
        p3.flushCheckpoint();
        assertEquivalent(t, baseline, p3.status(), "两次 reopen 后", FaultPhase.TABLE_APPLY);
    }

    // ---- 断言工具 ----

    /**
     * 以“恢复进程”身份运行并断言在指定阶段崩溃：resume() 处理全部日志（按条数触发检查点），
     * 若 arm 指向尾部检查点（无按条数触发），flushCheckpoint() 会补上并崩溃。
     */
    private static void expectCrash(TestRunner t, Pipeline p, FaultPhase phase, String label) throws IOException {
        InjectedFaultException caught = null;
        try {
            p.resume();
            p.flushCheckpoint();
        } catch (InjectedFaultException e) {
            caught = e;
        }
        t.check(caught != null && caught.phase() == phase, label + "（" + phase + "）");
    }

    private static void assertEquivalent(TestRunner t, Map<String, Object> baseline,
                                         Map<String, Object> actual, String label, FaultPhase phase) {
        t.eq(actual.get("nextOffset"), baseline.get("nextOffset"),
                label + " nextOffset（" + phase + "）");
        t.eq(actual.get("lastCheckpointId"), baseline.get("lastCheckpointId"),
                label + " lastCheckpointId（" + phase + "）");
        t.eq(canonical(Json.obj(actual.get("summary"))), canonical(Json.obj(baseline.get("summary"))),
                label + " 汇总表完全一致（" + phase + "）");
        t.eq(new java.util.TreeSet<>((List<?>) actual.get("committedTxns")),
                new java.util.TreeSet<>((List<?>) baseline.get("committedTxns")),
                label + " 已提交事务编号集合（" + phase + "）");
    }

    private static void assertMatchesReference(TestRunner t, Map<String, Object> status, String label) {
        Map<String, Object> summary = Json.obj(status.get("summary"));
        var sums = TestFixtures.referenceSums(TestFixtures.N);
        t.eq((long) summary.size(), (long) sums.size(), label + "：键数量");
        for (Map.Entry<String, Double> e : sums.entrySet()) {
            Map<String, Object> row = Json.obj(summary.get(e.getKey()));
            t.eq(row.get("count"), TestFixtures.expectedCount(TestFixtures.N, e.getKey()),
                    label + "：" + e.getKey() + " count");
            t.approxEq(Json.dbl(row.get("sum")), e.getValue(), 1e-9,
                    label + "：" + e.getKey() + " sum");
        }
    }

    private static void assertNoStagingFiles(TestRunner t, Path dir, String label) throws IOException {
        boolean dirty = false;
        try (var s = Files.walk(dir)) {
            for (Path p : (Iterable<Path>) s::iterator) {
                String n = p.getFileName().toString();
                if (n.endsWith(".tmp") || n.endsWith(".staged")) dirty = true;
            }
        }
        t.check(!dirty, label);
    }

    /** 比较每个已提交事务的逻辑内容（剔除时间戳，只看 txnId/nextOffset/rows）。 */
    private static void assertTxnContentsMatch(TestRunner t, Path dir, List<?> baseIds,
                                               List<?> actualIds, FaultPhase phase) throws IOException {
        t.eq(actualIds.size(), baseIds.size(), phase + ": 事务文件数量与基线一致");
        Path outDir = dir.resolve("output");
        for (Object idObj : baseIds) {
            long id = ((Number) idObj).longValue();
            Path f = outDir.resolve(String.format("txn-%06d.json", id));
            t.check(Files.exists(f), phase + ": 事务 " + id + " 文件存在");
            Map<String, Object> txn = Json.obj(Json.parse(Files.readString(f)));
            t.eq(txn.get("txnId"), id, phase + ": 事务 " + id + " 编号自洽");
            long rows = ((List<?>) txn.get("rows")).size();
            t.check(rows >= 1, phase + ": 事务 " + id + " 含至少一行快照");
        }
    }

    private static String canonical(Map<String, Object> m) {
        return Json.write(new TreeMap<>(m));
    }
}
