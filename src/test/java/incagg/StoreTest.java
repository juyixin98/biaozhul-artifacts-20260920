package incagg;

import incagg.model.Event;
import incagg.model.EventType;
import incagg.store.IncrementalViewStore;
import incagg.store.Snapshot;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static incagg.TestRunner.*;

/**
 * 核心验收测试：
 *  A. 维表分类变更 -> 订单行贡献跨类别迁移
 *  B. 重复业务事件 -> eventId 去重（含同载荷重放、冲突载荷重放、跨双边命名空间）
 *  C. 删除不存在记录 -> 幂等空操作
 *  D. 每一步都与全量重算比较（逐步差分）
 *  E. 定点数精度
 *  F. 确定性随机差分测试（2000+ 步，每 50 步与全量重算比对）
 *  G. HTTP 端到端冒烟（起真实端口打 curl 等价请求）—— 见 HttpSmokeTest
 */
public final class StoreTest {

    private static IncrementalViewStore store;

    public static void main(String[] args) {
        System.out.println("== A. 维表分类变更 + 全量重算逐步比对 ==");
        testCategoryChange();

        System.out.println("== B. 重复业务事件去重 ==");
        testDuplicateEvents();
        testDedupScopeGlobal();

        System.out.println("== C. 删除不存在记录（幂等） ==");
        testDeleteMissing();

        System.out.println("== D. 孤儿订单：先单后品 / 删品 / 重插 ==");
        testOrphanLifecycle();

        System.out.println("== E. 定点数金额精度 ==");
        testFixedPoint();

        System.out.println("== F. 随机差分测试（增量 vs 全量重算） ==");
        testRandomDifferential();

        System.exit(finish());
    }

    // ---------------------------------------------------------------- A

    static void testCategoryChange() {
        store = new IncrementalViewStore();
        // 商品 1=BOOKS，两条订单行引用它
        apply(productUpsert("e1", 1, "BOOKS"));
        apply(orderUpsert("e2", 101, 1, 2, "10.00"));
        apply(orderUpsert("e3", 102, 1, 3, "5.50"));

        check("初始: BOOKS qty=5 amount=15.50", () -> {
            Snapshot s = store.snapshot();
            assertEquals(5, cell(s, "BOOKS").qty, "qty");
            assertEqualsMoney("15.50", cell(s, "BOOKS").amount, "amount");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });

        // 维表分类变更 BOOKS -> MEDIA
        check("分类变更事件被应用", () -> {
            var r = store.apply(productUpsert("e4", 1, "MEDIA"));
            assertEquals("APPLIED", r.status, "status");
            assertTrue(!r.ignored, "不应被忽略");
        });
        check("变更后: BOOKS 消失，MEDIA qty=5 amount=15.50", () -> {
            Snapshot s = store.snapshot();
            assertTrue(!s.categories().containsKey("BOOKS"), "BOOKS 应被清除");
            assertEquals(5, cell(s, "MEDIA").qty, "qty");
            assertEqualsMoney("15.50", cell(s, "MEDIA").amount, "amount");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });

        // 再来一条订单 + 再迁一次，验证多次变更不串账
        apply(orderUpsert("e5", 103, 1, 1, "100.00"));
        apply(productUpsert("e6", 1, "TOYS"));
        check("二次迁移: TOYS qty=6 amount=115.50，MEDIA 清空", () -> {
            Snapshot s = store.snapshot();
            assertTrue(!s.categories().containsKey("MEDIA"), "MEDIA 应被清除");
            assertEquals(6, cell(s, "TOYS").qty, "qty");
            assertEqualsMoney("115.50", cell(s, "TOYS").amount, "amount");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
    }

    // ---------------------------------------------------------------- B

    static void testDuplicateEvents() {
        store = new IncrementalViewStore();
        apply(orderUpsert("dup-1", 201, 9, 1, "9.99")); // 商品 9 不存在 -> 孤儿

        // 同 eventId、同载荷重放
        check("同载荷重复事件: DUPLICATE / conflict=false", () -> {
            var r = store.apply(orderUpsert("dup-1", 201, 9, 1, "9.99"));
            assertEquals("DUPLICATE", r.status, "status");
            assertTrue(!r.conflict, "不应冲突");
            assertEquals(1, store.snapshot().orderCount(), "订单数不应增加");
        });

        // 同 eventId、不同载荷重放：首达获胜，新载荷绝不生效
        check("冲突载荷重复事件: DUPLICATE / conflict=true，首达载荷保持", () -> {
            var r = store.apply(orderUpsert("dup-1", 999, 8, 7, "77.00"));
            assertEquals("DUPLICATE", r.status, "status");
            assertTrue(r.conflict, "应标记冲突");
            // 首达事件的订单行仍是 productId=9 qty=1 amount=9.99
            Snapshot s = store.snapshot();
            assertEquals(1, s.orderCount(), "订单数");
            assertEquals(1, s.orphanQty(), "孤儿数量（仍按 productId=9）");
            assertEqualsMoney("9.99", s.orphanAmount(), "孤儿金额");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });

        // 批量接口里夹重放：首条应用，其余跳过
        check("批量内重复: 仅首条 APPLIED", () -> {
            var r1 = store.apply(productUpsert("dup-2", 40, "CAT_X"));
            var r2 = store.apply(productUpsert("dup-2", 40, "CAT_X"));
            assertEquals("APPLIED", r1.status, "r1");
            assertEquals("DUPLICATE", r2.status, "r2");
        });
    }

    /** 去重范围：eventId 全局共享，订单事件与商品事件同 ID 也算重复。 */
    static void testDedupScopeGlobal() {
        store = new IncrementalViewStore();
        apply(productUpsert("G-1", 70, "A"));
        check("跨双边同 eventId 仍去重（全局命名空间）", () -> {
            var r = store.apply(orderUpsert("G-1", 301, 70, 1, "1.00"));
            assertEquals("DUPLICATE", r.status, "status");
            assertEquals(0, store.snapshot().orderCount(), "订单不应插入");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
    }

    // ---------------------------------------------------------------- C

    static void testDeleteMissing() {
        store = new IncrementalViewStore();
        check("删除不存在的订单行: APPLIED 但 ignored=true（幂等），且 eventId 留痕", () -> {
            var r = store.apply(orderDelete("d1", 404));
            assertEquals("APPLIED", r.status, "status");
            assertTrue(r.ignored, "应为空操作");
            // 再来同 eventId 的删除 -> DUPLICATE，证明空操作事件也占去重名额
            var r2 = store.apply(orderDelete("d1", 404));
            assertEquals("DUPLICATE", r2.status, "重复删除应被去重");
        });
        check("删除不存在的商品: 幂等空操作", () -> {
            var r = store.apply(productDelete("d2", 405));
            assertEquals("APPLIED", r.status, "status");
            assertTrue(r.ignored, "ignored");
            assertTrue(store.diffAgainstFull().isEmpty(), "视图不变即与全量一致");
        });

        // 存在 -> 删除 -> 再删（不同 eventId）：第二次幂等
        apply(productUpsert("d3", 50, "C"));
        apply(orderUpsert("d4", 501, 50, 2, "2.00"));
        check("二次删除（新 eventId）同样幂等无副作用", () -> {
            var r1 = store.apply(productDelete("d5", 50));
            assertTrue(!r1.ignored, "首次删除应生效（订单变孤儿）");
            var r2 = store.apply(productDelete("d6", 50));
            assertTrue(r2.ignored, "第二次删除为空操作");
            assertEquals(2, store.snapshot().orphanQty(), "孤儿数量保持 2");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
    }

    // ---------------------------------------------------------------- D

    static void testOrphanLifecycle() {
        store = new IncrementalViewStore();
        // 先有订单，商品后到
        apply(orderUpsert("o1", 601, 60, 4, "40.00"));
        check("无维表: 计入孤儿", () -> {
            assertEquals(4, store.snapshot().orphanQty(), "orphanQty");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
        apply(productUpsert("p1", 60, "FOOD"));
        check("商品到达: 孤儿回归类别", () -> {
            Snapshot s = store.snapshot();
            assertEquals(0, s.orphanQty(), "orphanQty 清零");
            assertEquals(4, cell(s, "FOOD").qty, "FOOD qty");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
        apply(productDelete("p2", 60));
        check("商品删除: 再次成为孤儿", () -> {
            assertEquals(4, store.snapshot().orphanQty(), "orphanQty");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
        apply(orderDelete("o2", 601));
        check("孤儿订单删除: 孤儿清零", () -> {
            assertEquals(0, store.snapshot().orphanQty(), "orphanQty");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
    }

    // ---------------------------------------------------------------- E

    static void testFixedPoint() {
        store = new IncrementalViewStore();
        apply(productUpsert("m1", 80, "MONEY"));
        // 0.10 x 3 与 0.30：BigDecimal 定点，杜绝 double 的 0.30000000000000004
        apply(orderUpsert("m2", 801, 80, 1, "0.10"));
        apply(orderUpsert("m3", 802, 80, 1, "0.10"));
        apply(orderUpsert("m4", 803, 80, 1, "0.10"));
        check("0.10*3 == 0.30（定点，无浮点漂移）", () -> {
            assertEqualsMoney("0.30", cell(store.snapshot(), "MONEY").amount, "amount");
        });
        // 金额更新：旧值精确扣减
        apply(orderUpsert("m5", 801, 80, 1, "0.25"));
        check("更新后 0.25+0.10+0.10 == 0.45", () -> {
            assertEqualsMoney("0.45", cell(store.snapshot(), "MONEY").amount, "amount");
            assertTrue(store.diffAgainstFull().isEmpty(), "与全量重算一致");
        });
    }

    // ---------------------------------------------------------------- F

    static void testRandomDifferential() {
        store = new IncrementalViewStore();
        Random rnd = new Random(20260923L); // 确定性种子
        int productCount = 6;
        int orderCount = 30;
        int steps = 2000;
        long eventSeq = 0;
        List<String> usedIds = new ArrayList<>(); // 实际提交过 apply 的 eventId

        for (int step = 1; step <= steps; step++) {
            int kind = rnd.nextInt(10);
            long seq = eventSeq++;
            switch (kind) {
                case 0, 1, 2 -> { // 订单 upsert
                    long oid = 1 + rnd.nextInt(orderCount);
                    long pid = 1 + rnd.nextInt(productCount + 2); // 部分 pid 无维表 -> 孤儿
                    int qty = rnd.nextInt(10);
                    String amt = new BigDecimal(rnd.nextInt(100000))
                            .movePointLeft(2).toPlainString();
                    runQuietly(orderUpsert("r" + seq, oid, pid, qty, amt));
                    usedIds.add("r" + seq);
                }
                case 3 -> { // 订单删除
                    long oid = 1 + rnd.nextInt(orderCount + 5); // 部分删不到
                    runQuietly(orderDelete("r" + seq, oid));
                    usedIds.add("r" + seq);
                }
                case 4, 5, 6 -> { // 商品 upsert，含分类变更
                    long pid = 1 + rnd.nextInt(productCount);
                    String cat = "CAT" + rnd.nextInt(4);
                    runQuietly(productUpsert("r" + seq, pid, cat));
                    usedIds.add("r" + seq);
                }
                case 7 -> { // 商品删除
                    long pid = 1 + rnd.nextInt(productCount + 2);
                    runQuietly(productDelete("r" + seq, pid));
                    usedIds.add("r" + seq);
                }
                case 8 -> { // 重放：重复一个实际应用过的 eventId
                    if (!usedIds.isEmpty()) {
                        String oldId = usedIds.get(rnd.nextInt(usedIds.size()));
                        var r = replayAny(oldId);
                        if (!"DUPLICATE".equals(r.status))
                            throw new AssertionError("重放 " + oldId + " 应为 DUPLICATE，实际 " + r.status);
                    }
                }
                default -> { /* 9: 空步 */ }
            }

            if (step % 50 == 0) {
                List<String> diffs = store.diffAgainstFull();
                if (!diffs.isEmpty()) throw new AssertionError("第 " + step + " 步差分失败: " + diffs);
            }
        }
        check("2000 步随机事件后增量视图与全量重算一致", () -> {
            List<String> diffs = store.diffAgainstFull();
            assertTrue(diffs.isEmpty(), "最终差分: " + diffs);
        });
    }

    /** 用旧 eventId 构造一条订单 upsert 重放（载荷可能与首次不同 -> conflict 也应跳过）。 */
    private static IncrementalViewStore.ApplyResult replayAny(String oldId) {
        long oldSeq = Long.parseLong(oldId.substring(1));
        Random r = new Random(oldSeq * 31 + 7);
        long oid = 1 + r.nextInt(30);
        long pid = 1 + r.nextInt(8);
        return store.apply(orderUpsert(oldId, oid, pid, r.nextInt(10),
                new BigDecimal(r.nextInt(100000)).movePointLeft(2).toPlainString()));
    }

    // ---------------------------------------------------------------- 构造/辅助

    private static Event orderUpsert(String id, long oid, long pid, int qty, String amt) {
        return Event.builder(id, EventType.ORDER_UPSERT)
                .orderLineId(oid).productId(pid).qty(qty).amount(new BigDecimal(amt)).build();
    }
    private static Event orderDelete(String id, long oid) {
        return Event.builder(id, EventType.ORDER_DELETE).orderLineId(oid).build();
    }
    private static Event productUpsert(String id, long pid, String cat) {
        return Event.builder(id, EventType.PRODUCT_UPSERT).productId(pid).category(cat).build();
    }
    private static Event productDelete(String id, long pid) {
        return Event.builder(id, EventType.PRODUCT_DELETE).productId(pid).build();
    }

    private static void apply(Event e) {
        var r = store.apply(e);
        if (!"APPLIED".equals(r.status))
            throw new AssertionError("初始化事件意外被去重: " + e.eventId);
    }

    private static void runQuietly(Event e) {
        store.apply(e); // 随机流中 APPLIED/DUPLICATE/ignored 都合法
    }

    private static IncrementalViewStore.AggCell cell(Snapshot s, String cat) {
        IncrementalViewStore.AggCell c = s.categories().get(cat);
        if (c == null) throw new AssertionError("类别不存在: " + cat + "，现有: " + s.categories().keySet());
        return c;
    }
}
