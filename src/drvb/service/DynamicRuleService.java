package drvb.service;

import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.stream.EventProcessor;
import drvb.time.Clock;
import drvb.time.ManualClock;
import drvb.time.ManualScheduler;
import drvb.time.Scheduler;
import drvb.version.Binding;
import drvb.version.ReclamationService;
import drvb.version.RuleVersion;
import drvb.version.RuleVersionTable;
import drvb.version.VersionException;
import drvb.version.Watermark;

import java.util.List;
import java.util.Map;

/**
 * 动态规则版本绑定应用服务（门面）。
 *
 * <p>聚合版本绑定表、水位线、回收服务（含 GC 前提）、事件流处理器和可注入调度器，
 * 提供热更新、回滚、版本查询、事件提交、回收、手动时钟/调度控制等一整套用例。
 * 所有状态变更与查询均经由这里，便于并发控制与审计。
 */
public final class DynamicRuleService {

    private final RuleVersionTable table;
    private final Watermark watermark;
    private final ReclamationService reclamation;
    private final EventProcessor processor;
    private final Clock clock;
    private final Scheduler scheduler;
    private final ManualClock manualClock;
    private final ManualScheduler manualScheduler;

    public DynamicRuleService(Clock clock,
                              Scheduler scheduler,
                              long allowedLateness,
                              long retentionHorizon) {
        this.clock = clock;
        this.scheduler = scheduler;
        this.table = new RuleVersionTable(clock);
        this.watermark = new Watermark(allowedLateness);
        this.reclamation = new ReclamationService(table, watermark, retentionHorizon);
        this.processor = new EventProcessor(table, watermark, clock);
        this.manualClock = (clock instanceof ManualClock) ? (ManualClock) clock : null;
        this.manualScheduler = (scheduler instanceof ManualScheduler)
                ? (ManualScheduler) scheduler : null;
    }

    // ---------------------------------------------------------------
    // 规则热更新 / 回滚
    // ---------------------------------------------------------------

    /** 发布不可变新版本并绑定新生效边界（事件时间）。 */
    public Binding publishVersion(String versionId, String name,
                                  long effectiveFrom, Map<String, Object> predicateSpec,
                                  String note) {
        RuleVersion v = new RuleVersion(versionId, name, clock.nowMillis(), predicateSpec);
        return table.publish(v, effectiveFrom, note);
    }

    /** 回滚：让已存在的历史版本从新边界起重新生效（追加绑定，旧区间不变）。 */
    public Binding rollback(String existingVersionId, long effectiveFrom, String note) {
        return table.rollback(existingVersionId, effectiveFrom, note);
    }

    // ---------------------------------------------------------------
    // 事件流处理
    // ---------------------------------------------------------------

    public ProcessResult submit(Event event) {
        return processor.process(event);
    }

    public List<ProcessResult> submitAll(List<Event> events) {
        return processor.processAll(events);
    }

    public List<ProcessResult> queryResults(EventProcessor.ResultFilter filter) {
        return processor.query(filter);
    }

    public List<ProcessResult> allResults() {
        return processor.results();
    }

    // ---------------------------------------------------------------
    // 历史版本回收（前提强制）
    // ---------------------------------------------------------------

    public ReclamationService.Eligibility eligibility(String versionId) {
        return reclamation.checkEligibility(versionId);
    }

    public void reclaim(String versionId) {
        reclamation.reclaim(versionId);
    }

    public List<String> reclaimEligible() {
        return reclamation.reclaimEligible();
    }

    public Map<String, ReclamationService.Eligibility> eligibilityView() {
        return reclamation.eligibilityView();
    }

    public long reclaimGate() {
        return reclamation.gate();
    }

    // ---------------------------------------------------------------
    // 状态只读访问
    // ---------------------------------------------------------------

    public RuleVersionTable table() {
        return table;
    }

    public long watermark() {
        return watermark.current();
    }

    public long allowedLateness() {
        return watermark.allowedLateness();
    }

    public long retentionHorizon() {
        return reclamation.retentionHorizon();
    }

    public long processingTime() {
        return clock.nowMillis();
    }

    public Clock clock() {
        return clock;
    }

    public Scheduler scheduler() {
        return scheduler;
    }

    // ---------------------------------------------------------------
    // 可注入时间 / 调度的手动控制（仅 manual 模式；HTTP 层据此暴露）
    // ---------------------------------------------------------------

    public boolean isManualTime() {
        return manualClock != null;
    }

    /** 手动推进时钟，随后立即执行到点的调度任务（catch-up）。返回触发的任务名。 */
    public List<String> advanceTimeAndRun(long targetTimeMillis) {
        if (manualClock == null || manualScheduler == null) {
            throw new VersionException("NOT_MANUAL_TIME",
                    "仅在 --clock=manual 模式下允许手动推进时间");
        }
        manualClock.advanceTo(targetTimeMillis);
        return manualScheduler.runDue();
    }

    /** 在当前时钟点显式触发一次调度（墙钟模式下用于测试/运维）。 */
    public List<String> runDueTasks() {
        if (manualScheduler != null) {
            return manualScheduler.runDue();
        }
        throw new VersionException("NOT_MANUAL_TIME",
                "墙钟模式的周期任务由调度线程自动执行，可查看 /admin/scheduler 状态");
    }

    /** 注册自动回收周期任务；返回任务句柄。 */
    public Scheduler.ScheduledTask enableAutoReclaim(long periodMillis) {
        return scheduler.scheduleFixedRate("auto-reclaim-versions",
                periodMillis, this::reclaimEligible);
    }
}
