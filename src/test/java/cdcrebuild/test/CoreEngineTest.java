package cdcrebuild.test;

import cdcrebuild.codec.Json;
import cdcrebuild.engine.CdcEngine;
import cdcrebuild.engine.SemanticException;
import cdcrebuild.model.Event;
import cdcrebuild.model.ValidationException;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/** 引擎层功能测试。 */
public final class CoreEngineTest {

    private CoreEngineTest() {
    }

    public static void run(TestRunner t) {
        t.test("提交前不可见，COMMIT 后整体可见", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", 1, "name", "a")));
                t.assertTrue(engine.snapshot().isEmpty(), "未提交事务不应产生任何表");
                engine.ingest(Events.commit(3, "t1"));
                Map<String, Object> row = engine.rowByKey("users", "1");
                t.assertEquals("a", row.get("name"), "提交后应能读到行");
            }
        });

        t.test("ROLLBACK 后事务内全部变更消失", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", 1, "name", "a")));
                engine.ingest(Events.insert(3, "t1", "users", Events.row("id", 2, "name", "b")));
                engine.ingest(Events.rollback(4, "t1"));
                t.assertTrue(engine.snapshot().isEmpty(), "回滚后不应有任何行");
                t.assertNull(engine.rowByKey("users", "1"), "回滚后按键查询应为空");
                List<CdcEngine.TxnRecord> log = engine.queryLog(0, Long.MAX_VALUE);
                t.assertEquals(1, log.size(), "审计日志应有一条事务");
                t.assertTrue(!log.get(0).committed, "该事务应为回滚");
            }
        });

        t.test("主键变更：旧键删除、新键生效、旧键查不到", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", 1, "name", "a")));
                engine.ingest(Events.commit(3, "t1"));

                engine.ingest(Events.begin(4, "t2"));
                Map<String, Object> oldRow = Events.row("id", 1, "name", "a");
                Map<String, Object> newRow = Events.row("id", 9, "name", "a2");
                engine.ingest(Events.update(5, "t2", "users", oldRow, newRow));
                t.assertNull(engine.rowByKey("users", "9"), "未提交时新键不可见");
                t.assertEquals("a", engine.rowByKey("users", "1").get("name"), "未提交时旧键仍在");
                engine.ingest(Events.commit(6, "t2"));
                t.assertNull(engine.rowByKey("users", "1"), "提交后旧键必须消失");
                t.assertEquals("a2", engine.rowByKey("users", "9").get("name"), "新键必须生效");
            }
        });

        t.test("字符串主键的 UPDATE 不改键", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", "x", "v", 1)));
                engine.ingest(Events.commit(3, "t1"));
                engine.ingest(Events.begin(4, "t2"));
                engine.ingest(Events.update(5, "t2", "users",
                        Events.row("id", "x", "v", 1), Events.row("id", "x", "v", 2)));
                engine.ingest(Events.commit(6, "t2"));
                t.assertEquals(2L, ((Number) engine.rowByKey("users", "\"x\"").get("v")).longValue(),
                        "同键更新应生效");
            }
        });

        t.test("重复位置内容一致 => 幂等返回 DUPLICATE", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", 1)));
                engine.ingest(Events.commit(3, "t1"));
                CdcEngine.IngestResult r = engine.ingest(Events.commit(3, "t1"));
                t.assertEquals("DUPLICATE", r.status, "重复 COMMIT 应返回 DUPLICATE");
                t.assertEquals(1, engine.tableSnapshot("users").size(), "重复事件不得重复应用");
            }
        });

        t.test("重复位置内容冲突 => 409 拒绝", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(2, "t1", "users", Events.row("id", 1)));
                // 同位置但内容不同
                Event evil = Event.fromMap(Map.of(
                        "pos", 2L, "txId", "t1", "type", "DATA",
                        "table", "users", "op", "INSERT",
                        "new", Map.of("id", 666L)));
                t.assertThrows(SemanticException.class, () -> engine.ingest(evil),
                        "同位置不同内容必须拒绝");
            }
        });

        t.test("发现缺口：后续事件缓冲且状态 PAUSED，不跳过", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                // 直接投 pos=3，跳过 2
                CdcEngine.IngestResult r3 = engine.ingest(Events.insert(
                        3, "t1", "users", Events.row("id", 1)));
                t.assertEquals("BUFFERED", r3.status, "缺口后的事件应被缓冲");
                t.assertEquals("PAUSED", r3.state, "发现缺口必须暂停");
                t.assertEquals(2L, r3.firstMissingPos.longValue(), "应报告首个缺失位置 2");
                t.assertTrue(engine.snapshot().isEmpty(), "缺口未补前不能有已提交数据");
                // 再投 pos=4 同样缓冲
                CdcEngine.IngestResult r4 = engine.ingest(Events.commit(4, "t1"));
                t.assertEquals("BUFFERED", r4.status, "pos=4 也应缓冲");
                t.assertEquals("PAUSED", engine.status().get("state"), "仍暂停");
            }
        });

        t.test("缺口补齐：缓冲事件按序排空，表最终一致", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                engine.ingest(Events.insert(3, "t1", "users", Events.row("id", 2, "n", "b")));
                engine.ingest(Events.commit(4, "t1"));
                // 此时缺 pos=2
                CdcEngine.IngestResult r2 = engine.ingest(Events.insert(
                        2, "t1", "users", Events.row("id", 1, "n", "a")));
                t.assertEquals("DURABLE", r2.status, "缺口事件落盘并触发排空");
                t.assertEquals("RUNNING", r2.state, "补齐后应恢复 RUNNING");
                t.assertEquals(2, engine.tableSnapshot("users").size(), "应提交两行");
                t.assertEquals("a", engine.rowByKey("users", "1").get("n"), "id=1 内容正确");
                t.assertEquals("b", engine.rowByKey("users", "2").get("n"), "id=2 内容正确");
            }
        });

        t.test("未 BEGIN 的 DATA 语义非法：在线冲突 409", () -> {
            try (CdcEngine engine = openEngine(t)) {
                t.assertThrows(SemanticException.class,
                        () -> engine.ingest(Events.insert(1, "ghost", "users", Events.row("id", 1))),
                        "没有进行中事务不能投递 DATA");
            }
        });

        t.test("缺口排空中的非法事件触发 409 并暂停，重投修正后恢复", () -> {
            try (CdcEngine engine = openEngine(t)) {
                // 正常 pos=1
                engine.ingest(Events.begin(1, "t1"));
                // 预投 pos=3、4
                engine.ingest(Events.insert(3, "t1", "users", Events.row("id", 2)));
                engine.ingest(Events.commit(4, "t1"));
                // 投一个 pos=2 的非法事件（事务不匹配：pos=2 用了不存在的事务）
                Event bad2 = Events.insert(2, "ghost", "users", Events.row("id", 1));
                t.assertThrows(SemanticException.class, () -> engine.ingest(bad2),
                        "排空中遇到非法事件必须抛 409");
                t.assertEquals("PAUSED", engine.status().get("state"),
                        "非法的缺口事件应让系统保持暂停");
                t.assertTrue(((String) engine.status().get("gapError")).contains("校验失败"),
                        "暂停原因应说明事件校验失败");
                // 重投正确的 pos=2（覆盖缓冲中的坏事件并继续排空 pos=3、4）
                CdcEngine.IngestResult fixed = engine.ingest(
                        Events.insert(2, "t1", "users", Events.row("id", 1)));
                t.assertEquals("RUNNING", fixed.state, "修正重投后应恢复");
                t.assertEquals(2, engine.tableSnapshot("users").size(), "提交完成，共两行");
            }
        });

        t.test("结构非法事件（缺主键列）在线投递 => 400", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "t1"));
                Event noPk = Event.fromMap(Events.row(
                        "pos", 2L, "txId", "t1", "type", "DATA",
                        "table", "users", "op", "INSERT",
                        "new", Events.row("name", "no-id")));
                t.assertThrows(ValidationException.class, () -> engine.ingest(noPk),
                        "新行缺主键列必须 400");
            }
        });

        t.test("两个事务交错提交：各自原子可见", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.begin(2, "b"));
                engine.ingest(Events.insert(3, "a", "users", Events.row("id", 1, "g", "a")));
                engine.ingest(Events.insert(4, "b", "users", Events.row("id", 2, "g", "b")));
                engine.ingest(Events.commit(5, "a"));
                t.assertEquals("a", engine.rowByKey("users", "1").get("g"), "a 提交后可见");
                t.assertNull(engine.rowByKey("users", "2"), "b 未提交不可见");
                engine.ingest(Events.commit(6, "b"));
                t.assertEquals("b", engine.rowByKey("users", "2").get("g"), "b 提交后也可见");
                t.assertEquals(2, engine.tableSnapshot("users").size(), "共两行");
                t.assertEquals(2, engine.queryLog(0, Long.MAX_VALUE).size(), "两条审计记录");
            }
        });

        t.test("INSERT 主键冲突在 COMMIT 时报错（源端不应产生这种流）", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1)));
                engine.ingest(Events.commit(3, "a"));
                engine.ingest(Events.begin(4, "b"));
                engine.ingest(Events.insert(5, "b", "users", Events.row("id", 1)));
                t.assertThrows(SemanticException.class,
                        () -> engine.ingest(Events.commit(6, "b")),
                        "重复主键的提交必须失败");
            }
        });

        t.test("pkChanged 标记与实际主键不一致 => 语义冲突 409", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1, "v", 1)));
                engine.ingest(Events.commit(3, "a"));
                engine.ingest(Events.begin(4, "b"));
                Event wrong = Event.fromMap(Map.of(
                        "pos", 5L, "txId", "b", "type", "DATA",
                        "table", "users", "op", "UPDATE",
                        "old", Map.of("id", 1L), "new", Map.of("id", 1L),
                        "pkChanged", true));
                t.assertThrows(SemanticException.class, () -> engine.ingest(wrong),
                        "主键未变但 pkChanged=true 必须拒绝");
            }
        });

        t.test("结构非法事件 => ValidationException(400)", () -> {
            t.assertThrows(ValidationException.class,
                    () -> Event.fromMap(Map.of("pos", -1L, "txId", "a", "type", "BEGIN")),
                    "pos 必须为正整数");
            t.assertThrows(ValidationException.class,
                    () -> Event.fromMap(Map.of("pos", 1L, "txId", "a", "type", "DATA",
                            "table", "users", "op", "INSERT")),
                    "INSERT 缺 new");
            t.assertThrows(ValidationException.class,
                    () -> Event.fromMap(Map.of("pos", 1L, "txId", "a", "type", "WHAT")),
                    "未知 type");
        });

        t.test("DELETE 后可重新 INSERT 同键", () -> {
            try (CdcEngine engine = openEngine(t)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1, "v", "old")));
                engine.ingest(Events.commit(3, "a"));
                engine.ingest(Events.begin(4, "b"));
                engine.ingest(Events.delete(5, "b", "users", Events.row("id", 1, "v", "old")));
                engine.ingest(Events.insert(6, "b", "users", Events.row("id", 1, "v", "new")));
                engine.ingest(Events.commit(7, "b"));
                t.assertEquals("new", engine.rowByKey("users", "1").get("v"), "删后重插内容应为新值");
            }
        });
    }

    private static CdcEngine openEngine(TestRunner t) {
        try {
            Path dir = Events.tempDir("cdc-core-");
            return CdcEngine.open(dir, "id");
        } catch (Exception e) {
            throw new AssertionError("无法打开引擎: " + e);
        }
    }
}
