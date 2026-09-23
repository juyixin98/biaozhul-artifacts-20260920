package com.example.txflow;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * 进程内单元/集成测试（不触发 halt 崩溃；崩溃场景见 CrashRecoveryTest 的子进程方案）。
 *
 * 覆盖：
 *  - JSON 解析/输出
 *  - 输入日志追加与重读、offset 语义
 *  - 本地事务接收器：prepare 后不可见、commit 后可见、幂等 commit、recover 清理
 *  - 引擎正常路径：处理、状态、偏移、输出逐 offset 对齐
 *  - 引擎“软回滚”路径：prepared 持久化后由测试直接调用 recover（模拟提交前崩溃后重启），
 *    验证重放无重复
 */
public class InProcessTest {

    public static void main(String[] args) throws Exception {
        testJson();
        testInputLog();
        testSinkVisibility();
        testSinkRecoverHidesUncommitted();
        testEngineHappyPath();
        testEngineReplayAfterPreparedRollback();
        testEngineReplayAfterCommittedPromotion();
        testIdempotentReprocessIsEmpty();
        System.exit(Assert.summary());
    }

    static Path tmpDir(String prefix) throws Exception {
        return Files.createTempDirectory(prefix);
    }

    static void testJson() {
        System.out.println("== Json ==");
        Map<String, Object> m = Json.parseObject("{\"a\": 1, \"b\": [true, false, null, -3.5, \"x\\n\"]}");
        Assert.equals("int 解析", 1, ((Number) m.get("a")).intValue());
        List<?> list = (List<?>) m.get("b");
        Assert.equals("bool/null/double/string", "x\n", list.get(4));
        Assert.equals("double 解析", -3.5, ((Number) list.get(3)).doubleValue());
        String round = Json.write(m);
        Assert.equals("往返解析保持结构", "x\n",
                ((List<?>) Json.parseObject(round).get("b")).get(4));
    }

    static void testInputLog() throws Exception {
        System.out.println("== InputLog ==");
        Path dir = tmpDir("txflow-input-");
        InputLog log = new InputLog(dir);
        Assert.equals("空日志 offset 0", 0L, log.countRecords());
        Assert.equals("append 返回 offset 0", 0L, log.append("{\"text\":\"a\"}"));
        Assert.equals("append 返回 offset 1", 1L, log.append("{\"text\":\"b\"}"));
        List<String> recs = log.readAll();
        Assert.equals("读到 2 条", 2, recs.size());
        Assert.equals("第 0 条", "{\"text\":\"a\"}", recs.get(0));

        // 重新打开（模拟重启）
        InputLog log2 = new InputLog(dir);
        Assert.equals("重启后仍 2 条", 2L, log2.countRecords());
    }

    static void testSinkVisibility() throws Exception {
        System.out.println("== LocalTxnSink 可见性与幂等 ==");
        Path dir = tmpDir("txflow-sink-");
        LocalTxnSink sink = new LocalTxnSink(dir);
        byte[] out1 = "line-A\n".getBytes();
        String d1 = sink.prepare(0, out1);

        // prepare 之后、commit 之前：没有标记，可见输出必须为空
        Assert.equals("prepare 后可见输出为空", 0, sink.readCommittedOutputs().size());
        Assert.equals("prepare 后 isCommitted=false", false, sink.isCommitted(0));

        sink.commit(0, 0, d1);
        Assert.equals("commit 后 1 个可见事务", 1, sink.readCommittedOutputs().size());
        Assert.equals("可见内容正确", "line-A\n",
                new String(sink.readCommittedOutputs().get(0)));

        // 幂等 commit：重放时重复提交不得产生第二条标记/输出
        sink.commit(0, 0, d1);
        Assert.equals("重复 commit 仍只有 1 条标记", 1, sink.readMarkers().size());

        // 第二个事务
        String d2 = sink.prepare(1, "line-B\n".getBytes());
        sink.commit(1, 2, d2);
        Assert.equals("2 个事务按序可见", 2, sink.readCommittedOutputs().size());
        Assert.equals("第 2 个 endOffset", 2L, sink.readMarkers().get(1).endOffset);

        // 未 commit 不能有输出文件泄漏到可见集合：prepare txn2 但不 commit，重开 recover
        String d3 = sink.prepare(2, "line-C\n".getBytes());
        // 重新构造 sink 并 recover（不调 recover 时 readCommittedOutputs 仍只看标记）
        Assert.equals("第 3 个 prepare 后仍只见 2 个", 2, sink.readCommittedOutputs().size());
        Map<String, Object> report = sink.recover();
        Assert.equals("recover 后仍 2 个可见", 2, sink.readCommittedOutputs().size());
        @SuppressWarnings("unchecked")
        List<String> actions = (List<String>) report.get("actions");
        Assert.check("recover 报告隐藏了未提交文件",
                actions.stream().anyMatch(a -> a.contains("txn-2.out")));
        Assert.equals("未提交文件已被隐藏", false,
                Files.exists(sink.outputsDir().resolve("txn-2.out")));
    }

    static void testSinkRecoverHidesUncommitted() throws Exception {
        System.out.println("== LocalTxnSink staging 残留清理 ==");
        Path dir = tmpDir("txflow-sink2-");
        LocalTxnSink sink = new LocalTxnSink(dir);
        sink.prepare(0, "x\n".getBytes());
        sink.commit(0, 0, LocalTxnSink.sha256("x\n".getBytes()));
        // 手工制造 staging 残留（prepare 写文件后 rename 前崩溃）
        Files.write(sink.outputsDir().resolve("../staging/txn-9.out").normalize(),
                "orphan\n".getBytes());
        Map<String, Object> report = sink.recover();
        @SuppressWarnings("unchecked")
        List<String> actions = (List<String>) report.get("actions");
        Assert.check("清理了 staging 孤儿文件",
                actions.stream().anyMatch(a -> a.contains("txn-9.out")));
        Assert.equals("已提交输出不受影响", 1, sink.readCommittedOutputs().size());
    }

    static void testEngineHappyPath() throws Exception {
        System.out.println("== Engine 正常路径 ==");
        Path dir = tmpDir("txflow-eng-");
        Engine engine = new Engine(dir);
        engine.recover();
        engine.inputLog().append("{\"text\":\"a b a\"}");
        engine.inputLog().append("{\"text\":\"a c\"}");

        Map<String, Object> r1 = engine.process(1, Engine.FailPoint.NONE);
        Assert.equals("批1处理1条", 1L, ((Number) r1.get("processed")).longValue());
        Assert.equals("批1 endOffset=0", 0L, ((Number) r1.get("endOffset")).longValue());

        Map<String, Object> r2 = engine.process(10, Engine.FailPoint.NONE);
        Assert.equals("批2处理剩余1条", 1L, ((Number) r2.get("processed")).longValue());

        List<String> outs = engine.visibleOutputs();
        Assert.equals("共 2 条可见输出", 2, outs.size());
        long[] offs = outs.stream().mapToLong(
                l -> ((Number) Json.parseObject(l).get("inputOffset")).longValue()).toArray();
        Assert.equals("输出 offset 序列 [0,1]", List.of(0L, 1L),
                List.of(offs[0], offs[1]));

        Map<String, Object> counts = countsOf(outs.get(1));
        Assert.equals("最终计数 a=3", 3L, ((Number) counts.get("a")).longValue());
        Assert.equals("最终计数 b=1", 1L, ((Number) counts.get("b")).longValue());
        Assert.equals("最终计数 c=1", 1L, ((Number) counts.get("c")).longValue());

        // 重启：状态/偏移/标记持久化
        Engine engine2 = new Engine(dir);
        engine2.recover();
        Assert.equals("重启后 lastCommittedOffset=1", 1L,
                ((Number) engine2.snapshotState().get("lastCommittedOffset")).longValue());
        Assert.equals("重启后可见输出仍 2 条", 2, engine2.visibleOutputs().size());
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> countsOf(String outputLine) {
        return (Map<String, Object>) ((Map<String, Object>)
                Json.parseObject(outputLine).get("stateAfter")).get("counts");
    }

    /**
     * 模拟“提交前崩溃后重启”：
     * 快照里有 prepared、接收器无标记 → recover 回滚，重放必须重建出相同输出且无重复。
     * （真正的 JVM halt 场景由 CrashRecoveryTest 子进程覆盖。）
     */
    static void testEngineReplayAfterPreparedRollback() throws Exception {
        System.out.println("== Engine prepared 回滚后重放 ==");
        Path dir = tmpDir("txflow-rollback-");
        Engine engine = new Engine(dir);
        engine.recover();
        engine.inputLog().append("{\"text\":\"a a\"}");
        engine.process(1, Engine.FailPoint.NONE);       // txn0 提交
        engine.inputLog().append("{\"text\":\"a b\"}");

        // 手工制造“状态落盘后、提交前崩溃”的现场：
        // 直接把快照写成含 prepared（与 Engine 阶段3后等价），并保留 prepare 好的输出文件。
        Checkpoint cp = new Checkpoint.Store(dir).load();
        Assert.equals("当前 lastCommitted=0", 0L, cp.lastCommittedOffset);
        // 用内部流程：prepare 输出并写 prepared 快照（不走 commit）
        WordCountOperator op = new WordCountOperator();
        java.util.Map<String, Object> state =
                WordCountOperator.deepCopy(cp.stateCommitted);
        java.util.Map<String, Object> out =
                op.processOne(state, "{\"text\":\"a b\"}", 1);
        byte[] bytes = (Json.write(out) + "\n").getBytes();
        // 通过反射拿到 sink 太重，直接 new 一个同目录 sink
        LocalTxnSink sink2 = new LocalTxnSink(dir);
        String digest = sink2.prepare(1, bytes);
        Checkpoint.PreparedTxn p = new Checkpoint.PreparedTxn();
        p.txnId = 1;
        p.beginOffset = 1;
        p.endOffset = 1;
        p.outputFile = LocalTxnSink.outputName(1);
        p.outputDigest = digest;
        p.stateAfter = state;
        cp.prepared = p;
        new Checkpoint.Store(dir).save(cp);

        // 此时输出文件存在但无标记 → 不可见
        Assert.equals("崩溃现场：可见输出仅 1 条", 1, sink2.readCommittedOutputs().size());

        // 新引擎恢复：应回滚 prepared 并清理未提交文件
        Engine engine2 = new Engine(dir);
        Map<String, Object> report = engine2.recover();
        @SuppressWarnings("unchecked")
        List<String> actions = (List<String>) report.get("checkpointActions");
        Assert.check("报告了 prepared 回滚",
                actions.stream().anyMatch(a -> a.contains("丢弃 prepared")));
        Assert.equals("回滚后 lastCommitted=0", 0L,
                ((Number) engine2.snapshotState().get("lastCommittedOffset")).longValue());

        // 重放
        engine2.process(10, Engine.FailPoint.NONE);
        List<String> outs = engine2.visibleOutputs();
        Assert.equals("重放后恰好 2 条可见输出（无重复无遗漏）", 2, outs.size());
        long[] offs = outs.stream().mapToLong(
                l -> ((Number) Json.parseObject(l).get("inputOffset")).longValue()).toArray();
        Assert.equals("offset 序列仍为 [0,1]", List.of(0L, 1L), List.of(offs[0], offs[1]));
        Assert.equals("计数正确 a=3,b=1", 3L,
                ((Number) countsOf(outs.get(1)).get("a")).longValue());
    }

    /**
     * 模拟“commit 后、快照提升前崩溃”：快照有 prepared、接收器已有标记
     * → recover 直接提升，无需重放。
     */
    static void testEngineReplayAfterCommittedPromotion() throws Exception {
        System.out.println("== Engine committed 后崩溃的快照提升 ==");
        Path dir = tmpDir("txflow-promote-");
        Engine engine = new Engine(dir);
        engine.recover();
        engine.inputLog().append("{\"text\":\"m\"}");
        engine.process(1, Engine.FailPoint.NONE); // txn0
        engine.inputLog().append("{\"text\":\"m m\"}");

        Checkpoint.Store store = new Checkpoint.Store(dir);
        Checkpoint cp = store.load();
        WordCountOperator op = new WordCountOperator();
        java.util.Map<String, Object> state = WordCountOperator.deepCopy(cp.stateCommitted);
        java.util.Map<String, Object> out = op.processOne(state, "{\"text\":\"m m\"}", 1);
        byte[] bytes = (Json.write(out) + "\n").getBytes();
        LocalTxnSink sink2 = new LocalTxnSink(dir);
        String digest = sink2.prepare(1, bytes);
        sink2.commit(1, 1, digest); // 标记已写
        Checkpoint.PreparedTxn p = new Checkpoint.PreparedTxn();
        p.txnId = 1;
        p.beginOffset = 1;
        p.endOffset = 1;
        p.outputFile = LocalTxnSink.outputName(1);
        p.outputDigest = digest;
        p.stateAfter = state;
        cp.prepared = p;
        store.save(cp); // 快照仍停在 prepared（commit 后崩溃现场）

        Engine engine2 = new Engine(dir);
        engine2.recover();
        Assert.equals("提升后 lastCommitted=1", 1L,
                ((Number) engine2.snapshotState().get("lastCommittedOffset")).longValue());
        Assert.equals("提升后 committedCount=2", 2L,
                ((Number) engine2.snapshotState().get("committedCount")).longValue());
        List<String> outs = engine2.visibleOutputs();
        Assert.equals("恰好 2 条可见输出", 2, outs.size());
        // 再 process 不应重放
        Map<String, Object> idle = engine2.process(10, Engine.FailPoint.NONE);
        Assert.equals("无新记录可处理", 0L, ((Number) idle.get("processed")).longValue());
        Assert.equals("输出仍 2 条", 2, engine2.visibleOutputs().size());
    }

    static void testIdempotentReprocessIsEmpty() throws Exception {
        System.out.println("== 无新输入时空转 ==");
        Path dir = tmpDir("txflow-idle-");
        Engine engine = new Engine(dir);
        engine.recover();
        Map<String, Object> r = engine.process(5, Engine.FailPoint.NONE);
        Assert.equals("空输入处理 0 条", 0L, ((Number) r.get("processed")).longValue());
        Assert.equals("空输入无可见输出", 0, engine.visibleOutputs().size());
    }
}
