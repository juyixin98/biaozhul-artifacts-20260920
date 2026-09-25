package drvb.version;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 历史版本回收服务——集中实现并强制"回收前提"。
 *
 * <h3>前提定义</h3>
 * 设当前水位线为 WM，历史版本保留视窗为 retentionHorizon（毫秒）。
 * 一个<b>非当前</b>存活版本 {@code v} 仅当满足以下全部条件时才允许回收：
 * <ol>
 *   <li>v 的所有生效区间都已闭合（不包含向右无限延伸的当前区间）；</li>
 *   <li>v 的最晚区间结束时间 {@code endMax} 满足
 *       {@code endMax &lt;= WM - retentionHorizon}。</li>
 * </ol>
 * 含义：即使允许乱序（allowedLateness），也额外保留 retentionHorizon 的时长；
 * 在此之前可能还有晚到事件需要 v 来求值。前提不满足时回收请求被拒绝，
 * 绝不提前删除——这是"历史版本回收前提"的核心保证。
 *
 * <p>边界时刻采用 {@code <=} 精确相等判定，便于验收在 gate 时刻做边界测试。
 *
 * <h3>回收后的行为</h3>
 * 规则体被移除、版本成为墓碑（绑定历史保留），此后解析到该版本的事件以
 * {@code RECLAIMED_RULE_VERSION} 被拒绝，而不是被悄悄改判为最新规则。
 */
public final class ReclamationService {

    private final RuleVersionTable table;
    private final Watermark watermark;
    private final long retentionHorizon;

    public ReclamationService(RuleVersionTable table, Watermark watermark, long retentionHorizon) {
        if (retentionHorizon < 0) {
            throw new IllegalArgumentException("retentionHorizon 不能为负");
        }
        this.table = table;
        this.watermark = watermark;
        this.retentionHorizon = retentionHorizon;
    }

    public long retentionHorizon() {
        return retentionHorizon;
    }

    /** 回收闸门：WM - retentionHorizon（水位线尚未初始化时为负无穷）。 */
    public long gate() {
        long wm = watermark.current();
        if (wm == Long.MIN_VALUE) {
            return Long.MIN_VALUE;
        }
        return wm - retentionHorizon;
    }

    /**
     * 判断版本是否满足回收前提（不执行回收）。
     *
     * @return 判定结果（含不可回收时的人类可读原因）
     */
    public Eligibility checkEligibility(String versionId) {
        RuleVersionTable.Snapshot snap = table.snapshot();
        if (!snap.aliveVersions().containsKey(versionId)) {
            String reason = snap.tombstones().contains(versionId)
                    ? "版本已被回收" : "版本不存在";
            return new Eligibility(false, null, reason);
        }
        String currentId = snap.bindings().isEmpty() ? null
                : snap.bindings().get(snap.bindings().size() - 1).ruleVersionId();
        if (versionId.equals(currentId)) {
            return new Eligibility(false, null, "版本是当前生效版本（存在向右无限区间），不可回收");
        }
        List<RuleVersionTable.Interval> intervals = table.intervalsOf(versionId);
        Long endMax = null;
        for (RuleVersionTable.Interval iv : intervals) {
            if (iv.toExclusive() == null) {
                return new Eligibility(false, null, "版本仍存在未闭合的生效区间");
            }
            if (endMax == null || iv.toExclusive() > endMax) {
                endMax = iv.toExclusive();
            }
        }
        long g = gate();
        if (g == Long.MIN_VALUE) {
            return new Eligibility(false, endMax, "水位线尚未初始化（尚未观测任何事件）");
        }
        if (endMax == null) {
            return new Eligibility(false, null, "版本没有任何生效绑定");
        }
        if (endMax > g) {
            return new Eligibility(false, endMax,
                    "回收前提不满足：最晚区间结束 " + endMax + " > 回收闸门 "
                            + g + "（WM " + watermark.current() + " - 保留视窗 "
                            + retentionHorizon + "），仍可能有需要该版本的晚到事件");
        }
        return new Eligibility(true, endMax,
                "前提满足：最晚区间结束 " + endMax + " <= 回收闸门 " + g);
    }

    /**
     * 执行回收；前提不满足时抛 {@link VersionException}（{@code RECLAIM_NOT_ELIGIBLE}）。
     */
    public void reclaim(String versionId) {
        Eligibility e = checkEligibility(versionId);
        if (!e.eligible()) {
            throw new VersionException("RECLAIM_NOT_ELIGIBLE",
                    "版本 " + versionId + " 不可回收：" + e.reason());
        }
        table.markReclaimed(versionId);
    }

    /**
     * 扫描全部存活的非当前版本，回收所有满足前提者，返回被回收的版本 id 列表。
     * 供周期性自动回收任务调用。
     */
    public List<String> reclaimEligible() {
        RuleVersionTable.Snapshot snap = table.snapshot();
        List<String> reclaimed = new ArrayList<>();
        for (String id : snap.aliveVersions().keySet()) {
            if (checkEligibility(id).eligible()) {
                table.markReclaimed(id);
                reclaimed.add(id);
            }
        }
        return reclaimed;
    }

    /** 全部存活版本的可回收性快照（管理/状态接口用）。 */
    public Map<String, Eligibility> eligibilityView() {
        RuleVersionTable.Snapshot snap = table.snapshot();
        Map<String, Eligibility> out = new LinkedHashMap<>();
        for (String id : snap.aliveVersions().keySet()) {
            out.put(id, checkEligibility(id));
        }
        return out;
    }

    /** 可回收性判定结果。 */
    public static final class Eligibility {
        private final boolean eligible;
        private final Long latestIntervalEnd;
        private final String reason;

        public Eligibility(boolean eligible, Long latestIntervalEnd, String reason) {
            this.eligible = eligible;
            this.latestIntervalEnd = latestIntervalEnd;
            this.reason = reason;
        }

        public boolean eligible() {
            return eligible;
        }

        public Long latestIntervalEnd() {
            return latestIntervalEnd;
        }

        public String reason() {
            return reason;
        }
    }
}
