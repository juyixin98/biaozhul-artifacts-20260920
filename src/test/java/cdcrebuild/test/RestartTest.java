package cdcrebuild.test;

import cdcrebuild.engine.CdcEngine;

import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 跨重启测试：
 *  1) 已提交事务重启后仍可见；半事务（只有 BEGIN/DATA、无 COMMIT）重启后仍 open 且不可见；
 *     重启后补 COMMIT 立即生效（半事务重放）。
 *  2) 回滚的半事务重启后补 ROLLBACK 仍然丢弃。
 *  3) WAL 末尾被截断半条记录时，打开自动修复，完整记录全部保留。
 *  4) 重启后重复投递旧位置仍幂等。
 */
public final class RestartTest {

    private RestartTest() {
    }

    public static void run(TestRunner t) {
        t.test("跨重启：已提交保留、半事务不可见、补 COMMIT 后生效", () -> {
            Path dir = Events.tempDir("cdc-restart-");

            try (CdcEngine engine = open(t, dir)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1, "v", 1)));
                engine.ingest(Events.commit(3, "a"));
                // 第二个事务停在半完成状态（模拟进程在 COMMIT 前崩溃）
                engine.ingest(Events.begin(4, "b"));
                engine.ingest(Events.insert(5, "b", "users", Events.row("id", 2, "v", 2)));
                t.assertEquals(1, engine.tableSnapshot("users").size(), "重启前只应有 a 的一行");
            }

            // ---- 进程重启：重新打开同一数据目录 ----
            try (CdcEngine engine = open(t, dir)) {
                t.assertEquals(5L, ((Number) engine.status().get("lastDurablePos")).longValue(),
                        "WAL 应重放到 pos=5");
                t.assertEquals(1, engine.tableSnapshot("users").size(),
                        "半事务 b 不可见，只有 a 的一行");
                t.assertEquals(1L, engine.rowByKey("users", "1").get("v"), "a 的数据保留");
                t.assertTrue(engine.status().get("openTxIds").toString().contains("b"),
                        "b 应仍是 open 事务");

                // 恢复后投递 pos=6
                engine.ingest(Events.commit(6, "b"));
                t.assertEquals(2, engine.tableSnapshot("users").size(), "补提交后 b 生效");
                t.assertEquals(2L, engine.rowByKey("users", "2").get("v"), "b 行内容正确");
                t.assertEquals(7L, engine.status().get("nextExpectedPos"), "nextExpected=7");
            }
        });

        t.test("跨重启：半事务补 ROLLBACK 仍丢弃", () -> {
            Path dir = Events.tempDir("cdc-restart-rb-");
            try (CdcEngine engine = open(t, dir)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1, "v", 1)));
                engine.ingest(Events.commit(3, "a"));
                engine.ingest(Events.begin(4, "b"));
                engine.ingest(Events.insert(5, "b", "users", Events.row("id", 2, "v", 2)));
            }
            try (CdcEngine engine = open(t, dir)) {
                engine.ingest(Events.rollback(6, "b"));
                t.assertEquals(1, engine.tableSnapshot("users").size(), "回滚半事务后仍只有 a");
                t.assertNull(engine.rowByKey("users", "2"), "b 的行不可见");
            }
        });

        t.test("WAL 半条尾部自动截断修复，完整记录不丢", () -> {
            Path dir = Events.tempDir("cdc-walcut-");
            Path walFile = dir.resolve("events.wal");
            long goodLen;
            try (CdcEngine engine = open(t, dir)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1)));
                engine.ingest(Events.commit(3, "a"));
                engine.ingest(Events.begin(4, "b"));
                engine.ingest(Events.insert(5, "b", "users", Events.row("id", 2)));
                goodLen = (Long) engine.status().get("walLengthBytes");
            }
            // 人为追加“垃圾字节”模拟崩溃时的半条写
            try (OutputStream out = Files.newOutputStream(walFile,
                    java.nio.file.StandardOpenOption.APPEND)) {
                out.write(new byte[]{0, 0, 0, 99, 1, 2, 3});
            } catch (java.io.IOException e) {
                throw new UncheckedIOException(e);
            }
            t.assertTrue(size(walFile) > goodLen, "确认已写入垃圾尾部");

            try (CdcEngine engine = open(t, dir)) {
                t.assertEquals(5L, ((Number) engine.status().get("lastDurablePos")).longValue(),
                        "修复后应重放全部 5 条完整记录");
                t.assertEquals(goodLen, size(walFile), "WAL 应被截回干净长度");
                t.assertEquals(1, engine.tableSnapshot("users").size(), "已提交数据保留");
                engine.ingest(Events.commit(6, "b"));
                t.assertEquals(2, engine.tableSnapshot("users").size(), "修复后新提交正常");
            }
        });

        t.test("重启后重复投递旧位置仍幂等", () -> {
            Path dir = Events.tempDir("cdc-restart-dup-");
            try (CdcEngine engine = open(t, dir)) {
                engine.ingest(Events.begin(1, "a"));
                engine.ingest(Events.insert(2, "a", "users", Events.row("id", 1)));
                engine.ingest(Events.commit(3, "a"));
            }
            try (CdcEngine engine = open(t, dir)) {
                CdcEngine.IngestResult r = engine.ingest(Events.commit(3, "a"));
                t.assertEquals("DUPLICATE", r.status, "重放同位置应幂等");
                t.assertEquals(1, engine.tableSnapshot("users").size(), "不重复应用");
            }
        });
    }

    private static CdcEngine open(TestRunner t, Path dir) {
        try {
            return CdcEngine.open(dir, "id");
        } catch (Exception e) {
            throw new AssertionError("无法打开引擎: " + e, e);
        }
    }

    private static long size(Path p) {
        try {
            return Files.size(p);
        } catch (java.io.IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
