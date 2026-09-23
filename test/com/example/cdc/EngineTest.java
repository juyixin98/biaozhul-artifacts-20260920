package com.example.cdc;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 引擎级语义测试（不走 HTTP）。 */
final class EngineTest {

    private static Path tmpDir() throws Exception {
        Path d = java.nio.file.Files.createTempDirectory("cdc-test-");
        d.toFile().deleteOnExit();
        return d;
    }

    private static Engine newEngine(Path dir) {
        Wal wal = Wal.open(dir.resolve("cdc.wal"));
        Engine e = new Engine(wal);
        e.recover();
        return e;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> rows(Engine e, String table) {
        return (List<Map<String, Object>>) e.snapshot().getOrDefault(table, new ArrayList<>());
    }

    private static Engine.IngestResult post(Engine e, Map<String, Object>... raw) {
        List<Event> events = new ArrayList<>();
        for (Map<String, Object> m : raw) {
            events.add(Event.fromMap(m));
        }
        return e.ingest(events);
    }

    public static void register() {

        Test.it("提交前不可见，COMMIT 后整体可见", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.insert(2, "T1", "users", List.of(2), Ev.v("name", "b")));
            Test.check(rows(e, "users").isEmpty(), "未提交事务的数据不可见");
            Test.check(Boolean.TRUE.equals(e.openTxn("T1").get("open")), "T1 应处于 OPEN");
            post(e, Ev.commit(3, "T1"));
            Test.eq(rows(e, "users").size(), 2, "提交后应有两行");
            Test.check(Boolean.FALSE.equals(e.openTxn("T1").get("open")), "T1 提交后已结束");
        });

        Test.it("事务回滚：DATA 全部丢弃", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.rollback(2, "T1"));
            Test.check(rows(e, "users").isEmpty(), "回滚后不应有任何行");
            // 回滚后同 txn 再用必须报错（事务已结束）
            try {
                post(e, Ev.commit(3, "T1"));
                Test.fail("对已回滚事务 COMMIT 应被拒绝");
            } catch (Engine.ProtocolException ok) {
                // 期望
            }
        });

        Test.it("主键变更：UPDATE oldPk 删除旧主键并写入新主键", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("id", 1L, "name", "a")),
                    Ev.commit(2, "T1"));
            post(e, Ev.pkChange(3, "T2", "users", List.of(1), List.of(9),
                            Ev.v("id", 9L, "name", "a2")),
                    Ev.commit(4, "T2"));
            List<Map<String, Object>> rows = rows(e, "users");
            Test.eq(rows.size(), 1, "改主键不应产生重复行");
            Test.eq(rows.get(0).get("id"), 9L, "新主键行应存在");
            Test.eq(rows.get(0).get("name"), "a2", "列值应更新");
        });

        Test.it("复合主键：JSON 数组，规范化字符串判等", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "edges", List.of("a", 1), Ev.v("w", 10L)),
                    Ev.commit(2, "T1"));
            post(e, Ev.update(3, "T2", "edges", List.of("a", 1), Ev.v("w", 11L)),
                    Ev.commit(4, "T2"));
            Test.eq(rows(e, "edges").size(), 1, "复合主键应判为同一行");
            Test.eq(rows(e, "edges").get(0).get("w"), 11L, "复合主键行可更新");
        });

        Test.it("重复位置幂等：重发历史事件不重复生效", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"));
            // 历史 DATA + COMMIT 重放
            Engine.IngestResult r = post(e, Ev.insert(1, "T1", "users", List.of(1),
                    Ev.v("name", "a")), Ev.commit(2, "T1"));
            Test.eq(r.duplicates, 2, "两条都应识别为重复");
            Test.eq(rows(e, "users").size(), 1, "重复事件不得产生第二行");
        });

        Test.it("位置缺口：暂停而非跳过，补齐后自动恢复", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"));
            // 直接跳到 5（先发它，制造缺口）
            Engine.IngestResult r = post(e,
                    Ev.insert(5, "T9", "users", List.of(9), Ev.v("name", "z")));
            Test.check(r.paused, "出现缺口应 PAUSED");
            Test.eq(e.status().get("state"), Engine.PAUSED, "状态应为 PAUSED");
            Test.eq(e.status().get("nextExpected"), 3L, "下一个期望位置为 3");
            // 即使再来更大的位置 6 也只是滞留，绝不生效
            post(e, Ev.insert(6, "T9", "users", List.of(6), Ev.v("name", "six")));
            Test.eq(rows(e, "users").size(), 1, "滞留区事件不得生效");
            // 补 3、4：T2 提交，连续排空到 4；5 仍滞留（水位 4，缺的下一个正是 5）
            post(e, Ev.insert(3, "T2", "users", List.of(2), Ev.v("name", "b")));
            Test.eq(e.status().get("state"), Engine.PAUSED, "还差 4，仍暂停");
            post(e, Ev.commit(4, "T2"));
            // 5、6 本就在滞留区，补到 4 后自动连续排空（T9 变为 OPEN），状态恢复 RUNNING
            Test.eq(e.status().get("state"), Engine.RUNNING, "5/6 连续排空后应 RUNNING");
            Test.eq(rows(e, "users").size(), 2, "T2 提交后可见；T9 未提交仍不可见");
            Test.eq(e.status().get("nextExpected"), 7L, "下一个期望为 T9 的 COMMIT 7");
            post(e, Ev.commit(7, "T9"));
            Test.eq(e.status().get("state"), Engine.RUNNING, "提交 7 后仍 RUNNING");
            Test.eq(rows(e, "users").size(), 4, "T9 两行应可见");
        });

        Test.it("跨重启半事务：未提交数据重启后仍 OPEN 且不可见，随后可提交", () -> {
            Path dir = tmpDir();
            Engine e1 = newEngine(dir);
            post(e1, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.insert(2, "T1", "users", List.of(2), Ev.v("name", "b")));
            Test.eq(rows(e1, "users").size(), 0, "提交前不可见");

            // 模拟进程重启：丢弃内存，重放 WAL
            Engine e2 = newEngine(dir);
            Test.eq(rows(e2, "users").size(), 0, "重启后半事务仍不可见");
            Test.check(Boolean.TRUE.equals(e2.openTxn("T1").get("open")), "T1 重启后仍 OPEN");
            Test.eq(e2.openTxn("T1").get("dataCount"), 2, "T1 的两条 DATA 应完整恢复");
            post(e2, Ev.commit(3, "T1"));
            Test.eq(rows(e2, "users").size(), 2, "重启后提交应生效");
        });

        Test.it("跨重启重放：完整事务历史重建后与中断前一致，且可继续消费", () -> {
            Path dir = tmpDir();
            Engine e1 = newEngine(dir);
            post(e1,
                    Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"),
                    Ev.insert(3, "T2", "users", List.of(2), Ev.v("name", "b")),
                    Ev.update(4, "T2", "users", List.of(2), Ev.v("name", "b2")),
                    Ev.commit(5, "T2"),
                    Ev.insert(6, "T3", "users", List.of(3), Ev.v("name", "c")),
                    Ev.rollback(7, "T3"));
            Engine e2 = newEngine(dir); // 重启
            Test.eq(rows(e2, "users").size(), 2, "两行已提交数据应重建");
            Test.eq(rows(e2, "users").get(1).get("name"), "b2", "更新应保留");
            // 历史事件重发：全部幂等
            Engine.IngestResult r = post(e2,
                    Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"));
            Test.eq(r.duplicates, 2, "重启后历史事件仍按重复忽略");
            // 继续消费位置 8
            post(e2, Ev.delete(8, "T4", "users", List.of(1)), Ev.commit(9, "T4"));
            Test.eq(rows(e2, "users").size(), 1, "删除应在重放状态上继续生效");
            Map<String, Object> rec = e2.reconcile();
            Test.check(Boolean.TRUE.equals(rec.get("match")), "重放后必须与源解释器一致: " + rec);
        });

        Test.it("WAL 半行：模拟崩溃残写，重启自动截断且不丢完整记录", () -> {
            Path dir = tmpDir();
            Path walFile = dir.resolve("cdc.wal");
            Engine e1 = newEngine(dir);
            post(e1, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"));
            e1 = null;
            // 追加半条未完成记录（无换行、JSON 不完整）
            java.nio.file.Files.writeString(walFile,
                    "{\"position\":3,\"type\":\"DATA\",\"txn\":\"T2\",\"tab",
                    java.nio.file.StandardOpenOption.APPEND);
            Engine e2 = newEngine(dir);
            Test.eq(e2.status().get("watermark"), 2L, "残行应被截断，水位停在 2");
            Test.eq(rows(e2, "users").size(), 1, "完整记录不受影响");
            // 位置 3 可以重新投递
            post(e2, Ev.insert(3, "T2", "users", List.of(2), Ev.v("name", "b")),
                    Ev.commit(4, "T2"));
            Test.eq(rows(e2, "users").size(), 2, "截断后可正常继续");
        });

        Test.it("非法事件在落 WAL 前拒绝，不污染日志（可恢复）", () -> {
            Path dir = tmpDir();
            Engine e = newEngine(dir);
            try {
                post(e, Ev.commit(1, "GHOST"));
                Test.fail("COMMIT 未知事务必须拒绝");
            } catch (Engine.ProtocolException ok) {
                // 期望
            }
            Test.eq(e.status().get("watermark"), 0L, "拒绝后水位不变");
            // 重新用合法事件从位置 1 开始
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("name", "a")),
                    Ev.commit(2, "T1"));
            Test.eq(rows(e, "users").size(), 1, "拒绝非法事件后日志仍可正常使用");
            Engine e2 = newEngine(dir);
            Test.eq(e2.status().get("watermark"), 2L, "WAL 中不应有毒记录");
        });

        Test.it("台账记录含表、主键、旧/新值与源位置", () -> {
            Engine e = newEngine(tmpDir());
            post(e, Ev.insert(1, "T1", "users", List.of(1), Ev.v("id", 1L, "v", 10L)),
                    Ev.commit(2, "T1"));
            Map<String, Object> old = Ev.v("id", 1L, "v", 10L);
            post(e, Ev.data(3, "T2", "users", "UPDATE", List.of(1), null,
                    Ev.v("v", 11L)), Ev.commit(4, "T2"));
            List<Map<String, Object>> changes = e.ledger("users");
            Test.eq(changes.size(), 2, "两条已提交变更");
            Map<String, Object> upd = changes.get(1);
            Test.eq(upd.get("commitPosition"), 4L, "记录提交位置");
            Test.eq(upd.get("dataPosition"), 3L, "记录 DATA 位置");
            Test.eq(upd.get("table"), "users", "记录表名");
            Test.eq(upd.get("pk"), List.of(1), "记录主键");
            Test.eq(((Map<?, ?>) upd.get("oldValues")).get("v"), 10L, "记录旧值");
            Test.eq(((Map<?, ?>) upd.get("newValues")).get("v"), 11L, "记录新值");
            // old 变量仅用于语义说明
            Test.check(old.containsKey("v"), "");
        });

        Test.it("随机故障注入（交错事务/回滚/改主键/缺口/重发/重启）后对账一致",
                EngineTest::randomCrashScenario);

        Test.it("缺口暂停期间重启：WAL 乱序落盘，重放后仍 PAUSED 且滞留不丢，补齐恢复", () -> {
            Path dir = tmpDir();
            Engine e1 = newEngine(dir);
            post(e1, Ev.insert(1, "T1", "t", List.of(1), Ev.v("v", 1L)),
                    Ev.commit(2, "T1"));
            // 先发 5（缺口），再发 6，WAL 物理顺序 = ...2,5,6（相对逻辑序乱序落盘）
            post(e1, Ev.insert(5, "T9", "t", List.of(9), Ev.v("v", 9L)),
                    Ev.insert(6, "T9", "t", List.of(6), Ev.v("v", 6L)));
            Test.eq(e1.status().get("state"), Engine.PAUSED, "暂停中重启前置条件");
            // 在暂停状态下重启
            Engine e2 = newEngine(dir);
            Test.eq(e2.status().get("state"), Engine.PAUSED, "重放后必须仍是 PAUSED");
            Test.eq(e2.status().get("watermark"), 2L, "水位停在 2");
            Test.eq(e2.status().get("bufferedPositions"),
                    List.of(5L, 6L), "滞留事件 5、6 必须按位置有序恢复");
            Test.eq(rows(e2, "t").size(), 1, "滞留事件重启后仍不可见");
            // 补齐 3、4（T2 提交），5/6 自动排空进 OPEN T9，再 7 提交
            post(e2, Ev.insert(3, "T2", "t", List.of(2), Ev.v("v", 2L)),
                    Ev.commit(4, "T2"));
            post(e2, Ev.commit(7, "T9"));
            Test.eq(e2.status().get("state"), Engine.RUNNING, "补齐后恢复 RUNNING");
            Test.eq(rows(e2, "t").size(), 4, "四行齐全");
            Map<String, Object> rec = e2.reconcile();
            Test.check(Boolean.TRUE.equals(rec.get("match")),
                    "乱序 WAL 补齐后对账必须一致: " + rec.get("differences"));
        });

        Test.it("DELETE 后同主键再 INSERT，解释器与消费器一致", () -> {
            Engine e = newEngine(tmpDir());
            post(e,
                    Ev.insert(1, "T1", "t", List.of(1), Ev.v("x", 1L)),
                    Ev.commit(2, "T1"),
                    Ev.delete(3, "T2", "t", List.of(1)),
                    Ev.commit(4, "T2"),
                    Ev.insert(5, "T3", "t", List.of(1), Ev.v("x", 2L)),
                    Ev.commit(6, "T3"));
            Map<String, Object> rec = e.reconcile();
            Test.check(Boolean.TRUE.equals(rec.get("match")), "应一致: " + rec);
            Test.eq(rows(e, "t").get(0).get("x"), 2L, "删后再插应取新值");
        });
    }

    /** 确定性“随机”场景：用固定种子生成源事务，再制造缺口、重发、重启，最后与解释器对账。 */
    private static void randomCrashScenario() throws Exception {
        java.util.Random rnd = new java.util.Random(20260923L);
        List<Map<String, Object>> source = new ArrayList<>();
        long pos = 1;
        int txnCount = 24;
        for (int t = 1; t <= txnCount; t++) {
            String txn = "tx" + t;
            int ops = 1 + rnd.nextInt(4);
            for (int o = 0; o < ops; o++) {
                String table = "t" + (rnd.nextInt(2) + 1);
                long key = 1 + rnd.nextInt(8);
                int kind = rnd.nextInt(10);
                if (kind < 4) {
                    source.add(Ev.insert(pos++, txn, table, List.of(key),
                            Ev.v("id", key, "n", rnd.nextInt(100))));
                } else if (kind < 8) {
                    source.add(Ev.update(pos++, txn, table, List.of(key),
                            Ev.v("n", rnd.nextInt(100))));
                } else if (kind == 8) {
                    long newKey = 1 + rnd.nextInt(8);
                    source.add(Ev.pkChange(pos++, txn, table, List.of(key), List.of(newKey),
                            Ev.v("id", newKey, "n", rnd.nextInt(100))));
                } else {
                    source.add(Ev.delete(pos++, txn, table, List.of(key)));
                }
            }
            if (rnd.nextInt(5) == 0) {
                source.add(Ev.rollback(pos++, txn));
            } else {
                source.add(Ev.commit(pos++, txn));
            }
        }
        long total = pos - 1;

        Path dir = tmpDir();
        Engine engine = newEngine(dir);

        // remaining：尚未“首次发送”的事件，按位置排序
        TreeMapShim remaining = new TreeMapShim(source);
        List<Map<String, Object>> alreadySent = new ArrayList<>();
        int restarts = 0;
        int guard = 0;
        while (!remaining.isEmpty()
                || Engine.PAUSED.equals(engine.status().get("state"))
                || (Long) engine.status().get("watermark") < total) {
            if (++guard > 5000) {
                throw new AssertionError("随机场景循环未终止: " + engine.status());
            }
            if (Engine.PAUSED.equals(engine.status().get("state"))) {
                long need = (Long) engine.status().get("nextExpected");
                Map<String, Object> found = remaining.take(need);
                if (found == null) {
                    throw new AssertionError("缺口位置 " + need + " 无源事件可补，状态="
                            + engine.status());
                }
                post(engine, found); // 补齐；若其后连续会自动排空
                alreadySent.add(found);
                continue;
            }
            // RUNNING：取连续的一块发出；随机把块中某条及其后内容扣留造缺口
            int chunk = Math.min(remaining.size(), 1 + rnd.nextInt(4));
            List<Map<String, Object>> block = remaining.takeFirst(chunk);
            if (chunk >= 3 && rnd.nextInt(4) == 0) {
                int holdAt = 1 + rnd.nextInt(chunk - 2); // 扣留索引（>=1，保证至少发一条）
                // holdAt 及其后事件全部放回 remaining，待 PAUSED 时再补
                for (int k = block.size() - 1; k >= holdAt; k--) {
                    remaining.putBack(block.remove(k));
                }
            }
            if (rnd.nextInt(5) == 0 && !alreadySent.isEmpty()) {
                // 重发一条历史事件，验证幂等
                post(engine, alreadySent.get(rnd.nextInt(alreadySent.size())));
            }
            post(engine, block.toArray(new Map[0]));
            alreadySent.addAll(block);
            if (rnd.nextInt(6) == 0) {
                engine = newEngine(dir); // 重启（含暂停中重启）
                restarts++;
            }
        }
        // 所有滞留补齐后必须 RUNNING
        Test.eq(engine.status().get("state"), Engine.RUNNING, "最终应 RUNNING");
        Test.eq(engine.status().get("watermark"), total, "水位应到达源末尾 " + total);

        // 重启一次再对账
        engine = newEngine(dir);
        restarts++;
        Map<String, Object> rec = engine.reconcile();
        Test.check(Boolean.TRUE.equals(rec.get("match")),
                "随机场景对账失败: " + rec.get("differences"));
        Test.eq(((Number) rec.get("sourceEventCount")).longValue(), total, "源事件数");
        System.out.println("        (随机场景: " + total + " 条源事件, 重启 " + restarts + " 次)");
    }

    /** 按位置索引的“待发送”集合，支持取最小若干条 / 取走指定位置 / 放回。 */
    private static final class TreeMapShim {
        private final java.util.TreeMap<Long, Map<String, Object>> map = new java.util.TreeMap<>();

        TreeMapShim(List<Map<String, Object>> source) {
            for (Map<String, Object> m : source) {
                map.put((Long) m.get("position"), m);
            }
        }

        int size() {
            return map.size();
        }

        boolean isEmpty() {
            return map.isEmpty();
        }

        List<Map<String, Object>> takeFirst(int n) {
            List<Map<String, Object>> out = new ArrayList<>();
            for (int i = 0; i < n && !map.isEmpty(); i++) {
                out.add(map.pollFirstEntry().getValue());
            }
            return out;
        }

        Map<String, Object> take(long position) {
            return map.remove(position);
        }

        void putBack(Map<String, Object> m) {
            map.put((Long) m.get("position"), m);
        }
    }
}
