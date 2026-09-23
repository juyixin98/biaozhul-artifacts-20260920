package tumbling;

import java.util.List;
import java.util.Map;

import static tumbling.TestRunner.eq;
import static tumbling.TestRunner.assertTrue;
import static tumbling.TestRunner.lastEmission;
import static tumbling.TestRunner.countEmissions;

/** WindowEngine 语义单元测试。统一配置: 窗口 10，迟到容忍期 2（除非用例另行 reset）。 */
public final class EngineTest {

    private WindowEngine newEngine() {
        return new WindowEngine(10L, 2L);
    }

    /**
     * 用例 1 — 乱序 + 窗口触发时间 + 最终计数。
     * 事件时间 {2,7,5,11} 乱序进入 [0,10) 与 [10,20)。
     */
    @TestRunner.Test
    public void outOfOrderWithinWatermark() {
        WindowEngine e = newEngine();

        eq(e.ingest("a", "e2", 2).get("status"), "on_time", "t=2 正常");
        eq(e.ingest("a", "e7", 7).get("status"), "on_time", "t=7 正常");
        eq(e.ingest("a", "e5", 5).get("status"), "on_time", "t=5 乱序但水位未推进，正常");

        // 水位推到 9：end=10 尚未 <= 9，窗口不触发。
        eq(e.advanceWatermark("a", 9).get("globalWatermarkAfter"), 9L, "全局水位=9");
        assertTrue(lastEmission(e, "a", 0) == null, "wm=9 时 [0,10) 尚未触发");

        // 水位推到 10：[0,10) 触发，计数 3；但容忍期未过，不 final。
        Map<String, Object> w10 = e.advanceWatermark("a", 10);
        eq(w10.get("globalWatermarkAfter"), 10L, "全局水位=10");
        Map<String, Object> fired = lastEmission(e, "a", 0);
        assertTrue(fired != null && "fire".equals(fired.get("type")), "[0,10) 在 wm=10 时触发");
        eq(((Number) fired.get("count")).longValue(), 3L, "触发时计数=3");
        eq(((Number) fired.get("watermark")).longValue(), 10L, "触发发生在水位 10");

        // t=11 属于 [10,20)
        eq(e.ingest("a", "e11", 11).get("status"), "on_time", "t=11 正常");

        // 水位 11：晚到事件 t=6（< 水位11，晚到），但 6 < end+lateness=12，容忍期内 → 修订。
        Map<String, Object> late = e.ingest("a", "e6", 6);
        eq(late.get("status"), "late_accepted", "t=6 为容忍期内晚到事件");
        Map<String, Object> upd = lastEmission(e, "a", 0);
        eq(upd.get("type"), "update", "晚到修订产生 update 输出");
        eq(((Number) upd.get("count")).longValue(), 4L, "修订后计数=4");
        eq(((Number) upd.get("revision")).longValue(), 2L, "修订版本 revision=2");

        // 水位推到 12：[0,10) 最终关闭（end+lat=12 <= 12），最终计数 4。
        e.advanceWatermark("a", 12);
        Map<String, Object> fin = lastEmission(e, "a", 0);
        eq(fin.get("type"), "final", "[0,10) 在 wm=12 最终关闭");
        eq(((Number) fin.get("count")).longValue(), 4L, "最终计数=4");
        eq(countEmissions(e, "a", 0, "final"), 1L, "final 恰好一条");

        // t=1 再来：end+lat=12 <= 当前水位12 → 侧输出。
        Map<String, Object> dropped = e.ingest("a", "e1-late", 1);
        eq(dropped.get("status"), "late_dropped", "t=1 超过容忍期 → 侧输出");
        eq(e.sideOutputView().size(), 1L, "侧输出 1 条");
        eq(e.sideOutputView().get(0).get("eventId"), "e1-late", "侧输出内容正确");
    }

    /**
     * 用例 2 — 左闭右开边界，且容忍期为 0：事件时间恰好等于窗口末端的归属。
     */
    @TestRunner.Test
    public void halfOpenBoundaryZeroLateness() {
        WindowEngine e = new WindowEngine(10L, 0L);
        e.ingest("a", "at0", 0);
        e.ingest("a", "at9", 9);
        e.ingest("a", "at10", 10);
        e.ingest("a", "at19", 19);

        // wm=10 即触发并最终关闭 [0,10)（容忍期 0，fire 与 final 合并为一条 final）。
        e.advanceWatermark("a", 10);
        Map<String, Object> w0 = lastEmission(e, "a", 0);
        eq(w0.get("type"), "final", "容忍期 0 时 [0,10) 在 wm=10 直接 final");
        eq(((Number) w0.get("count")).longValue(), 2L, "[0,10) 含 t=0,9，不含 t=10");

        Map<String, Object> w10 = lastEmission(e, "a", 10);
        assertTrue(w10 == null, "[10,20) 在 wm=10 不触发（end=20）");

        e.advanceWatermark("a", 20);
        w10 = lastEmission(e, "a", 10);
        eq(w10.get("type"), "final", "[10,20) 在 wm=20 final");
        eq(((Number) w10.get("count")).longValue(), 2L, "[10,20) 含 t=10,19");

        // 容忍期 0：wm=20 后 t=15 立即侧输出。
        eq(e.ingest("a", "stale", 15).get("status"), "late_dropped", "零容忍下晚到直接侧输出");
    }

    /** 负时间戳按 floorDiv 归属窗口：[-10,0) 左闭右开，t=0 属于 [0,10)。 */
    @TestRunner.Test
    public void negativeTimestampFloorDiv() {
        WindowEngine e = new WindowEngine(10L, 0L);
        eq(((Number) e.ingest("a", "n1", -1).get("windowStart")).longValue(), -10L, "t=-1 → [-10,0)");
        eq(((Number) e.ingest("a", "n10", -10).get("windowStart")).longValue(), -10L, "t=-10 → [-10,0)");
        eq(((Number) e.ingest("a", "zero", 0).get("windowStart")).longValue(), 0L, "t=0 → [0,10)");
        e.advanceWatermark("a", 0);
        Map<String, Object> w = lastEmission(e, "a", -10);
        assertTrue(w != null, "[-10,0) 存在输出");
        eq(((Number) w.get("count")).longValue(), 2L, "[-10,0) 计数 2（-10 与 -1）");
    }

    /**
     * 用例 3a — 空闲分区：不参与全局水位最小值；恢复后重新被纳入。
     */
    @TestRunner.Test
    public void idlePartitionExcludedThenResumes() {
        WindowEngine e = newEngine();
        e.advanceWatermark("slow", 5);
        e.advanceWatermark("fast", 100);
        eq(e.getGlobalWatermark(), 5L, "两活跃分区，全局水位取最小值 5（先报慢、后报快）");

        e.setIdle("slow", true);
        eq(e.getGlobalWatermark(), 100L, "slow 空闲后，全局水位只剩 fast=100，立即推进到 100");

        // 空闲期间 fast 继续推进
        e.advanceWatermark("fast", 110);
        eq(e.getGlobalWatermark(), 110L, "空闲分区不阻挡水位推进");

        // slow 以 wm=60 恢复：min(110,60)=60，但全局水位单调不减，保持 110。
        Map<String, Object> resume = e.advanceWatermark("slow", 60);
        assertTrue((Boolean) resume.get("wasIdle"), "恢复前确实为空闲");
        eq(e.getGlobalWatermark(), 110L, "落后分区恢复不把全局水位拉回去");

        // slow 追到 120 但 fast 还在 110：全局 min=110。
        e.advanceWatermark("slow", 120);
        eq(e.getGlobalWatermark(), 110L, "落后者超过领先者之前，全局水位由领先者 110 决定");

        // fast 也到 120：全局水位恢复前进。
        e.advanceWatermark("fast", 120);
        eq(e.getGlobalWatermark(), 120L, "两分区都到 120 后全局水位=120");
    }

    /**
     * 用例 3b — 空闲分区恢复时携带旧时间戳事件：
     * 容忍期内 → 正常 late_accepted 修订；过于陈旧 → 侧输出。
     */
    @TestRunner.Test
    public void idleResumeLateAndSideOutput() {
        WindowEngine e = newEngine(); // 窗口10 容忍2
        // p1 两个窗口各放数据
        e.ingest("p1", "a1", 1);
        e.ingest("p1", "a2", 11);
        e.advanceWatermark("p1", 100);
        e.setIdle("p1", true);
        e.advanceWatermark("p2", 100);
        eq(e.getGlobalWatermark(), 100L, "全局水位推进到 100");

        // [0,10): end+lat = 12 < 100 → 已 purge；t=2 恢复事件 → 侧输出。
        Map<String, Object> stale = e.ingest("p1", "stale2", 2);
        eq(stale.get("status"), "late_dropped",
                "空闲期结束后到达的超陈旧事件进入侧输出");
        assertTrue((Boolean) stale.get("wasIdle"), "首条事件自动把 p1 从空闲恢复");

        // 构造“刚关闭仍在容忍期”的窗口：
        // p2 制造 wm=12（[0,10) final 后立即...），使用新分区精确卡点。
        WindowEngine e2 = newEngine();
        e2.ingest("z", "z1", 1);
        e2.advanceWatermark("z", 12); // [0,10) 此刻 final
        // final 之后事件 t=3：12 <= 12 → 超容忍，侧输出
        eq(e2.ingest("z", "z2", 3).get("status"), "late_dropped", "end+lateness 已过 → 侧输出");
        // wm=11 时（未 final，仅 fired）事件 t=3 可修订：
        WindowEngine e3 = newEngine();
        e3.ingest("z", "z1", 1);
        e3.advanceWatermark("z", 11); // 只 fire，不 close
        eq(e3.ingest("z", "z2", 3).get("status"), "late_accepted", "fired 但容忍期内 → 修订");
        eq(((Number) lastEmission(e3, "z", 0).get("count")).longValue(), 2L, "修订计数 2");
    }

    /**
     * 用例 4 — 重复事件：同分区同 eventId 不重复计数；窗口关闭后仍可识别重复。
     */
    @TestRunner.Test
    public void duplicatesNotCountedEvenAfterPurge() {
        WindowEngine e = new WindowEngine(10L, 0L);
        e.ingest("a", "dup", 5);
        e.ingest("a", "dup", 5);            // 完全重复
        Map<String, Object> diffTime = e.ingest("a", "dup", 8); // 同 id 不同时间，仍算重复
        eq(diffTime.get("status"), "duplicate", "同 eventId 不同时间戳也算重复");
        e.advanceWatermark("a", 10);
        eq(((Number) lastEmission(e, "a", 0).get("count")).longValue(), 1L, "窗口只计数一次");

        // 窗口 purge 后原 id 再来，仍识别为重复，且不进侧输出。
        Map<String, Object> after = e.ingest("a", "dup", 5);
        eq(after.get("status"), "duplicate", "purge 后重复事件依然被识别");
        eq(e.sideOutputView().size(), 0L, "重复事件不进入侧输出");
        eq(e.duplicatesView().size(), 3L, "重复记录共 3 条");

        // 不同分区同 id 不算重复。
        eq(e.ingest("b", "dup", 5).get("status"), "on_time", "去重作用域为单分区");
    }

    /** 多分区窗口各自独立计数；未初始化分区不被他人推高的全局水位关窗。 */
    @TestRunner.Test
    public void partitionsIndependent() {
        WindowEngine e = newEngine();
        e.ingest("a", "1", 1);
        e.ingest("a", "2", 2);
        e.ingest("b", "1", 3);
        // b 只发事件、从未上报水位：不参与全局 min，a 的水位可正常推进。
        e.advanceWatermark("a", 10);
        eq(e.getGlobalWatermark(), 10L, "未初始化水位的分区不参与全局 min，全局水位=10");
        eq(((Number) lastEmission(e, "a", 0).get("count")).longValue(), 2L, "a 窗口计数 2");
        assertTrue(lastEmission(e, "b", 0) == null, "b 尚未初始化水位，其窗口不被全局水位替它触发");

        // b 首次上报 10：其窗口此刻才触发，计数独立。
        e.advanceWatermark("b", 10);
        eq(e.getGlobalWatermark(), 10L, "b 首次上报 10，全局水位仍=10");
        eq(((Number) lastEmission(e, "b", 0).get("count")).longValue(), 1L, "b 窗口计数 1");
    }

    /**
     * 关键语义：窗口的触发/关闭时间【只由本分区自己的水位】决定。
     * 慢分区不能延迟快分区关窗；快分区也不能提前关掉慢分区的窗口。
     */
    @TestRunner.Test
    public void windowTimingDrivenByOwnPartitionWatermark() {
        WindowEngine e = newEngine(); // 窗口10 容忍2
        e.ingest("fast", "f1", 1);
        e.ingest("slow", "s1", 1);

        // fast 一路到 100：其 [0,10) 必须立即按自己的节奏 fire(10)/final(12)，
        // 即使 slow 从未上报水位、全局水位还停在 fast 的水位。
        e.advanceWatermark("fast", 10);
        Map<String, Object> fFire = lastEmission(e, "fast", 0);
        assertTrue(fFire != null && "fire".equals(fFire.get("type")), "fast 在自身水位10触发");
        eq(((Number) fFire.get("watermark")).longValue(), 10L, "触发水位戳=10");
        assertTrue(lastEmission(e, "slow", 0) == null, "slow 未上报水位，窗口不被替它触发");

        e.advanceWatermark("fast", 12);
        eq(lastEmission(e, "fast", 0).get("type"), "final", "fast 在自身水位12最终关闭");
        assertTrue(lastEmission(e, "slow", 0) == null, "slow 窗口仍不受影响");

        // slow 此时才上报水位 100：它的窗口按自己的水位一次性 final（fire/final 合并）。
        e.advanceWatermark("slow", 100);
        Map<String, Object> sOut = lastEmission(e, "slow", 0);
        eq(sOut.get("type"), "final", "slow 在自身水位100一次性最终关闭");
        eq(((Number) sOut.get("count")).longValue(), 1L, "slow 计数保持 1");
        // 全局水位此刻为 min(fast12, slow100)=12；slow 窗口未被全局水位提前关闭，
        // 所以 slow 在水位 12~100 之间到达的容忍期事件仍然有效。
        eq(e.getGlobalWatermark(), 12L, "全局水位取最小值 12");
    }

    /** 水位单调：更低的分区水位被钳制，全局水位永不回退。 */
    @TestRunner.Test
    public void watermarksMonotonic() {
        WindowEngine e = newEngine();
        e.advanceWatermark("a", 50);
        Map<String, Object> back = e.advanceWatermark("a", 20);
        assertTrue((Boolean) back.get("clamped"), "回退水位被钳制");
        eq(((Number) back.get("appliedWatermark")).longValue(), 50L, "实际水位仍为 50");
        eq(e.getGlobalWatermark(), 50L, "全局水位不回退");

        e.setIdle("a", true);
        eq(e.getGlobalWatermark(), 50L, "无任何活跃分区时全局水位保持不变");
        e.setIdle("a", false);
        eq(e.getGlobalWatermark(), 50L, "重新激活同样不回退");
    }

    /** 确定性：同样的事件/水位序列跑两遍，快照逐字段一致。 */
    @TestRunner.Test
    public void deterministicReplay() {
        String snap1 = Json.write(playScenario(newEngine()));
        String snap2 = Json.write(playScenario(newEngine()));
        eq(snap2, snap1, "两次重放快照必须完全一致");
    }

    /**
     * 回归 #1：已 final 并 purge 的窗口不能被新事件"复活"（否则会重复发 final、覆盖关闭历史）。
     */
    @TestRunner.Test
    public void purgedWindowIsNotResurrected() {
        WindowEngine e = newEngine(); // 窗口10 容忍2
        // q 先到 0：全局水位起点为 0。
        e.advanceWatermark("q", 0);
        e.ingest("p", "e1", 1);
        // p 自己推进到 12，其 [0,10) final 并 purge；但全局水位 min(q0,p12) 仍钳在 0。
        e.advanceWatermark("p", 12);
        eq(e.getGlobalWatermark(), 0L, "全局水位为 min(0,12)=0（单调，未上涨）");
        eq(countEmissions(e, "p", 0, "final"), 1L, "final 恰好一条");

        // 新 id 的事件 t=2：相对全局水位 0 "不晚到"，但本分区窗口已 final/purge。
        eq(e.ingest("p", "e2", 2).get("status"), "late_dropped",
                "已关闭窗口不复活，事件进侧输出");
        e.advanceWatermark("p", 13);
        eq(countEmissions(e, "p", 0, "final"), 1L, "没有产生第二条 final");
        // 关闭历史仍保留原始最终计数 1。
        Map<String, Object> last = lastEmission(e, "p", 0);
        eq(((Number) last.get("count")).longValue(), 1L, "关闭历史的最终计数未被覆盖");
        eq(e.sideOutputView().size(), 1L, "侧输出 1 条");
    }

    /** 回归 #6：接近 long 边界的时间戳/窗口溢出要抛出而非静默产生负窗口。 */
    @TestRunner.Test
    public void extremeTimestampOverflowRejected() {
        WindowEngine e = newEngine();
        boolean threw = false;
        try {
            e.ingest("a", "big", Long.MAX_VALUE);
        } catch (IllegalArgumentException ex) {
            threw = true;
        }
        assertTrue(threw, "Long.MAX_VALUE 时间戳导致窗口末端溢出，应被拒绝");
        // 正常值仍可用。
        eq(((Number) e.ingest("a", "ok", 1).get("windowEnd")).longValue(), 10L, "正常值不受影响");
    }

    /** 回归 #5：引擎返回的记录被外部修改不能污染内部日志/快照。 */
    @TestRunner.Test
    public void returnedRecordsAreDefensiveCopies() {
        WindowEngine e = newEngine();
        e.ingest("a", "1", 1);
        Map<String, Object> fired = e.advanceWatermark("a", 10);
        @SuppressWarnings("unchecked")
        Map<String, Object> out = (Map<String, Object>) ((List<Object>) fired.get("emitted")).get(0);
        out.put("count", 999);                 // 外部篡改返回值
        Map<String, Object> logged = lastEmission(e, "a", 0);
        eq(((Number) logged.get("count")).longValue(), 1L, "内部排放日志不受外部篡改影响");

        // 视图与快照同样是防御性拷贝。
        List<Map<String, Object>> v = e.emissionsView();
        v.get(0).put("type", "hacked");
        eq(lastEmission(e, "a", 0).get("type"), "fire", "emissionsView 拷贝隔离");
        e.sideOutputView().add(new java.util.LinkedHashMap<>(Map.of("x", 1)));
        eq(e.sideOutputView().size(), 0L, "sideOutputView 列表隔离");
    }

    private Map<String, Object> playScenario(WindowEngine e) {
        e.ingest("a", "1", 3);
        e.ingest("a", "2", 15);
        e.ingest("b", "1", 4);
        e.advanceWatermark("b", 12);
        e.advanceWatermark("a", 12);
        e.ingest("a", "1", 3);           // 重复
        e.ingest("a", "3", 2);           // 容忍期内
        e.advanceWatermark("b", 20);
        e.advanceWatermark("a", 20);
        e.ingest("a", "9", 1);           // 侧输出
        e.setIdle("a", true);
        e.advanceWatermark("b", 30);
        e.advanceWatermark("a", 30);     // 自动恢复
        return e.snapshot();
    }
}
