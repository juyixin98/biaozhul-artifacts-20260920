package cdcrebuild.test;

import cdcrebuild.codec.Json;
import cdcrebuild.engine.CdcEngine;
import cdcrebuild.model.Event;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.TreeSet;

/**
 * 随机化对账：生成一个包含交错事务、主键变更、回滚、多表的合法事件流，
 * 同时喂给“源事务解释器”和引擎，期间多次制造缺口、乱序和重启，
 * 最终两边的表必须完全一致。
 */
public final class RandomCompareTest {

    private static final String[] TABLES = {"users", "orders", "billing"};
    private static final int MAX_ID = 60;

    private RandomCompareTest() {
    }

    public static void run(TestRunner t) {
        // 多种子固定可复现；每个种子独立流、独立数据目录与 3 次重启
        long[] seeds = {20260923L, 7L, 42L, 999L, 314159L};
        for (long seed : seeds) {
            final long theSeed = seed;
            t.test("随机流对账（seed=" + theSeed + "）：乱序缺口 + 3 次重启 + 重放，最终与源解释器一致",
                    () -> runOneSeed(t, theSeed));
        }
    }

    private static void runOneSeed(TestRunner t, long seed) {
        List<Event> stream = Generator.generate(seed, 400);
        ReferenceInterpreter ref = new ReferenceInterpreter("id");
        for (Event e : stream) {
            ref.apply(e);
        }
        t.assertTrue(ref.openTxnsEmpty(), "生成器结尾应收掉所有事务");

        Path dir = Events.tempDir("cdc-random-");
        int n = stream.size();
        int[] cuts = {n / 5, (2 * n) / 5, (3 * n) / 5};
        int consumed = 0;
        consumed = consumeWithGaps(t, dir, stream, consumed, cuts[0], seed + 1);
        consumed = consumeWithGaps(t, dir, stream, consumed, cuts[1], seed + 2);
        consumed = consumeWithGaps(t, dir, stream, consumed, cuts[2], seed + 3);
        consumed = consumeWithGaps(t, dir, stream, consumed, n, seed + 4);
        t.assertEquals(n, consumed, "全部事件应已顺序消费");

        try (CdcEngine engine = openEngine(dir)) {
            t.assertEquals(Json.canonical(ref.snapshot()),
                    Json.canonical(engine.snapshot()),
                    "最终表必须与源事务解释器逐键逐字段一致");
            t.assertEquals(0, engine.status().get("openTxCount"), "不应残留 open 事务");
            t.assertEquals("RUNNING", engine.status().get("state"), "最终应处于 RUNNING");
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }

    private static CdcEngine openEngine(Path dir) {
        try {
            return CdcEngine.open(dir, "id");
        } catch (Exception e) {
            throw new AssertionError("无法打开引擎: " + e, e);
        }
    }

    /** 顺序消费 [start, limit)，随机制造乱序缺口并立即补齐；返回 limit。 */
    private static int consumeWithGaps(TestRunner t, Path dir, List<Event> stream,
                                       int start, int limit, long seed) {
        Random rnd = new Random(seed);
        try (CdcEngine engine = CdcEngine.open(dir, "id")) {
            int i = start;
            while (i < limit) {
                if (i + 2 < limit && rnd.nextInt(6) == 0) {
                    int skip = 1 + rnd.nextInt(Math.min(4, limit - i - 1));
                    for (int j = i + 1; j <= i + skip; j++) {
                        CdcEngine.IngestResult r = engine.ingest(stream.get(j));
                        t.assertEquals("BUFFERED", r.status, "乱序事件必须被缓冲");
                    }
                    t.assertEquals("PAUSED", engine.status().get("state"), "缺口期间必须暂停");
                    CdcEngine.IngestResult fixed = engine.ingest(stream.get(i));
                    t.assertEquals("DURABLE", fixed.status, "补齐事件应落盘");
                    t.assertEquals("RUNNING", fixed.state, "补齐后自动恢复消费");
                    i += skip + 1;
                } else {
                    CdcEngine.IngestResult r = engine.ingest(stream.get(i));
                    t.assertEquals("DURABLE", r.status, "顺序事件应落盘");
                    i++;
                }
            }
            t.assertEquals("RUNNING", engine.status().get("state"), "切点处应无缺口");
            return limit;
        } catch (Exception e) {
            throw new AssertionError("重启消费阶段异常: " + e, e);
        }
    }

    // ------------------------------------------------------------ 生成器

    /**
     * 生成器自带一份“id 可见性账本”（提交行 + 各在途事务预留），
     * 保证产出的流对引擎状态机始终合法（不产生主键冲突 / 目标不存在 / 预留冲突）。
     */
    static final class Generator {
        private final Random rnd;
        private final List<Event> out = new ArrayList<>();
        private long pos = 1;
        private int dataCount = 0;
        private int txSeq = 0;

        /** 每表已提交存活 id。 */
        private final List<TreeSet<Integer>> live = new ArrayList<>();
        /** 在途事务为“新插入/改主键的新键”预留的 id（按表），防止其它事务撞键。 */
        private final List<Set<Integer>> reserved = new ArrayList<>();
        /** 在途事务已经对其做过 UPDATE/DELETE 的已提交行（按表），防止第二个事务再操作同一行。 */
        private final List<Set<Integer>> claimedRows = new ArrayList<>();

        private final List<TxBuf> active = new ArrayList<>();

        static List<Event> generate(long seed, int targetDataEvents) {
            Generator g = new Generator(seed);
            g.run(targetDataEvents);
            return g.out;
        }

        Generator(long seed) {
            this.rnd = new Random(seed);
            for (int t = 0; t < TABLES.length; t++) {
                live.add(new TreeSet<>());
                reserved.add(new HashSet<>());
                claimedRows.add(new HashSet<>());
            }
        }

        private static final class TxBuf {
            final String txId;
            final boolean rollback;
            final List<Set<Integer>> inserts;
            final List<Set<Integer>> deletes;
            /** 本事务 UPDATE 过的“已提交行”，提交/回滚时从全局 claimed 释放。 */
            final List<Set<Integer>> claims;

            TxBuf(String txId, boolean rollback) {
                this.txId = txId;
                this.rollback = rollback;
                this.inserts = new ArrayList<>();
                this.deletes = new ArrayList<>();
                this.claims = new ArrayList<>();
                for (int t = 0; t < TABLES.length; t++) {
                    inserts.add(new HashSet<>());
                    deletes.add(new HashSet<>());
                    claims.add(new HashSet<>());
                }
            }

            Set<Integer> view(int table, TreeSet<Integer> committed, Set<Integer> globalClaimed) {
                Set<Integer> v = new TreeSet<>(committed);
                v.removeAll(globalClaimed); // 别的在途事务改/删过的行不再可见
                v.addAll(claims.get(table)); // 但本事务自己 UPDATE 过的行仍可继续操作
                v.removeAll(deletes.get(table));
                v.addAll(inserts.get(table));
                return v;
            }
        }

        private void run(int target) {
            while (dataCount < target) {
                if (active.isEmpty() || (active.size() < 3 && rnd.nextBoolean())) {
                    beginTx();
                }
                TxBuf tx = active.get(rnd.nextInt(active.size()));
                appendData(tx);
                if (rnd.nextInt(10) < 4) {
                    finishTx(tx);
                }
            }
            // 按开始顺序收掉剩余事务（流的位置顺序即结束顺序）
            for (TxBuf tx : new ArrayList<>(active)) {
                finishTx(tx);
            }
        }

        private void beginTx() {
            String txId = "tx" + (txSeq++);
            TxBuf tx = new TxBuf(txId, rnd.nextInt(10) < 3);
            active.add(tx);
            out.add(Events.begin(pos++, txId));
        }

        private void appendData(TxBuf tx) {
            int table = rnd.nextInt(TABLES.length);
            // 该事务在该表上可见的行
            Set<Integer> visible = tx.view(table, live.get(table), claimedRows.get(table));
            int roll = rnd.nextInt(3);
            if (visible.isEmpty() || roll == 0) {
                doInsert(tx, table);
            } else if (roll == 1) {
                doUpdate(tx, table, visible);
            } else {
                doDelete(tx, table, visible);
            }
            dataCount++;
        }

        private void doInsert(TxBuf tx, int table) {
            Integer id = freeId(table, tx);
            if (id == null) {
                return; // 极小概率：空间耗尽，跳过
            }
            tx.inserts.get(table).add(id);
            reserved.get(table).add(id);
            out.add(Events.insert(pos++, tx.txId, TABLES[table], row(id)));
        }

        private void doUpdate(TxBuf tx, int table, Set<Integer> visible) {
            int id = pick(visible);
            boolean changePk = rnd.nextInt(5) == 0;
            int newId = id;
            if (changePk) {
                Integer free = freeId(table, tx);
                if (free == null) {
                    changePk = false;
                } else {
                    newId = free;
                }
            }
            Map<String, Object> oldRow = row(id);
            Map<String, Object> newRow = row(newId);
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("pos", pos++);
            m.put("txId", tx.txId);
            m.put("type", "DATA");
            m.put("table", TABLES[table]);
            m.put("op", "UPDATE");
            m.put("old", oldRow);
            m.put("new", newRow);
            m.put("pkChanged", changePk);
            out.add(Event.fromMap(m));

            boolean ownInsert = tx.inserts.get(table).contains(id);
            if (changePk) {
                // 旧键从本事务视图消失；自插行释放其预留，已提交行加入全局 claimed
                tx.inserts.get(table).remove(id);
                tx.deletes.get(table).add(id);
                if (ownInsert) {
                    reserved.get(table).remove(id);
                } else {
                    claimedRows.get(table).add(id);
                }
                // 新键在本事务内可见且对其他事务预留
                tx.inserts.get(table).add(newId);
                reserved.get(table).add(newId);
            } else if (!ownInsert) {
                // 已提交行的非改键更新：全局 claim，但本事务视图通过 claims 仍可见
                claimedRows.get(table).add(id);
                tx.claims.get(table).add(id);
            }
        }

        private void doDelete(TxBuf tx, int table, Set<Integer> visible) {
            int id = pick(visible);
            out.add(Events.delete(pos++, tx.txId, TABLES[table], row(id)));
            if (tx.inserts.get(table).remove(id)) {
                // 删的是本事务自己插的行：释放预留即可
                reserved.get(table).remove(id);
            } else {
                tx.deletes.get(table).add(id);
                claimedRows.get(table).add(id);
            }
        }

        private void finishTx(TxBuf tx) {
            if (!active.remove(tx)) {
                return;
            }
            if (tx.rollback) {
                out.add(Events.rollback(pos++, tx.txId));
                for (int t = 0; t < TABLES.length; t++) {
                    // 回滚：所有预留、行占用全部恢复
                    reserved.get(t).removeAll(tx.inserts.get(t));
                    claimedRows.get(t).removeAll(tx.deletes.get(t));
                    claimedRows.get(t).removeAll(tx.claims.get(t));
                }
            } else {
                out.add(Events.commit(pos++, tx.txId));
                for (int t = 0; t < TABLES.length; t++) {
                    // 删除 / 改键旧键 / 非改键更新：从 claimed 释放（删除还要移出 live）
                    for (int id : tx.deletes.get(t)) {
                        live.get(t).remove(id);
                        claimedRows.get(t).remove(id);
                    }
                    claimedRows.get(t).removeAll(tx.claims.get(t));
                    // 插入 / 改键新键：进入 live 并释放预留
                    for (int id : tx.inserts.get(t)) {
                        live.get(t).add(id);
                        reserved.get(t).remove(id);
                    }
                }
            }
        }

        /** 选一个未被任何在途事务预留 / 占用、且本事务尚未插入或已删的 id。 */
        private Integer freeId(int table, TxBuf tx) {
            List<Integer> candidates = new ArrayList<>();
            for (int id = 1; id <= MAX_ID; id++) {
                if (!live.get(table).contains(id)
                        && !reserved.get(table).contains(id)
                        && !claimedRows.get(table).contains(id)
                        && !tx.inserts.get(table).contains(id)
                        && !tx.deletes.get(table).contains(id)) {
                    candidates.add(id);
                }
            }
            return candidates.isEmpty() ? null : candidates.get(rnd.nextInt(candidates.size()));
        }

        private int pick(Set<Integer> set) {
            int idx = rnd.nextInt(set.size());
            int i = 0;
            for (Integer v : set) {
                if (i++ == idx) {
                    return v;
                }
            }
            throw new IllegalStateException();
        }

        private Map<String, Object> row(int id) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", id);
            m.put("rev", (long) rnd.nextInt(1000));
            m.put("name", TABLES[0] + "-" + id + "-" + rnd.nextInt(50));
            return m;
        }
    }
}
