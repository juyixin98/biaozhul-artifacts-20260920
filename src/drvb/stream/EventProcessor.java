package drvb.stream;

import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.model.RejectReason;
import drvb.time.Clock;
import drvb.version.RuleVersion;
import drvb.version.RuleVersionTable;
import drvb.version.VersionException;
import drvb.version.Watermark;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 事件流计算核心算子（小数据精确参考实现）。
 *
 * <p>处理顺序（对每条输入事件）：
 * <ol>
 *   <li>从注入时钟取<b>处理时间</b>（到达时间），事件时间来自事件本身；</li>
 *   <li>先按事件时间解析版本（含"缺失版本/已回收版本"拒绝）；</li>
 *   <li>再观测水位线并判断晚到——晚到事件<b>不丢弃</b>，照常处理，
 *       且使用的是步骤 2 解析出的历史版本；</li>
 *   <li>在不可变版本谓词上求值，产出精确 {@link ProcessResult}；</li>
 *   <li>结果全量留存，可按状态查询，供参考比对。</li>
 * </ol>
 *
 * 版本解析必须在水位线观测之前完成：版本缺失/回收是拒绝，
 * 不应因为一条"本就无法判定"的事件而推进水位线。
 */
public final class EventProcessor {

    private final RuleVersionTable table;
    private final Watermark watermark;
    private final Clock clock;

    private final List<ProcessResult> results = new ArrayList<>();

    public EventProcessor(RuleVersionTable table, Watermark watermark, Clock clock) {
        this.table = table;
        this.watermark = watermark;
        this.clock = clock;
    }

    /** 处理单条事件。 */
    public ProcessResult process(Event event) {
        long processingTime = clock.nowMillis();
        long wmBefore = watermark.current();

        ProcessResult.Builder rb = ProcessResult.builder(event)
                .processingTime(processingTime)
                .watermarkBefore(wmBefore);

        // 1) 按事件时间解析不可变版本；失败 = 拒绝（绝不静默套用最新规则）
        RuleVersionTable.Resolved resolved;
        try {
            resolved = table.resolve(event.eventTime());
        } catch (VersionException e) {
            RejectReason reason = "RECLAIMED_RULE_VERSION".equals(e.code())
                    ? RejectReason.RECLAIMED_RULE_VERSION
                    : RejectReason.MISSING_RULE_VERSION;
            ProcessResult rejected = rb.watermarkAfter(wmBefore)
                    .late(watermark.isLate(event.eventTime()))
                    .rejected(reason, e.getMessage())
                    .build();
            record(rejected);
            return rejected;
        }

        // 2) 观测水位线（只在成功解析出版本后），判定晚到
        boolean late = watermark.isLate(event.eventTime());
        long wmAfter = watermark.observe(event.eventTime());

        // 3) 在对应（可能是历史的）不可变版本上求值
        RuleVersion version = resolved.version();
        Map<String, Object> context = buildContext(event);
        boolean matched = version.matches(context);

        ProcessResult result = rb
                .late(late)
                .watermarkAfter(wmAfter)
                .filtered(version.id(), resolved.binding().seq(), matched)
                .build();
        record(result);
        return result;
    }

    /** 批量处理（按给定顺序；乱序事件由调用方按到达顺序放入）。 */
    public List<ProcessResult> processAll(List<Event> events) {
        List<ProcessResult> out = new ArrayList<>(events.size());
        for (Event e : events) {
            out.add(process(e));
        }
        return out;
    }

    private void record(ProcessResult r) {
        synchronized (results) {
            results.add(r);
        }
    }

    /** 构造谓词求值上下文：payload 字段优先，保名字段回退注入。 */
    static Map<String, Object> buildContext(Event event) {
        Map<String, Object> ctx = new LinkedHashMap<>();
        ctx.put("eventId", event.eventId());
        ctx.put("eventTime", event.eventTime());
        if (event.type() != null) {
            ctx.put("type", event.type());
        }
        // payload 后放以覆盖保名占位（payload 同名键优先）
        ctx.putAll(event.payload());
        return ctx;
    }

    public long watermark() {
        return watermark.current();
    }

    /** 全部处理结果（到达顺序，不可变副本）。 */
    public List<ProcessResult> results() {
        synchronized (results) {
            return Collections.unmodifiableList(new ArrayList<>(results));
        }
    }

    /** 按状态过滤查询。 */
    public List<ProcessResult> query(ResultFilter filter) {
        synchronized (results) {
            List<ProcessResult> out = new ArrayList<>();
            int matchedCount = 0;
            for (ProcessResult r : results) {
                if (filter != null && filter.ruleVersionId() != null
                        && !filter.ruleVersionId().equals(r.ruleVersionId())) {
                    continue;
                }
                boolean keep;
                switch (filter == null ? ResultStatus.ALL : filter.status()) {
                    case ALL:
                        keep = true;
                        break;
                    case MATCHED:
                        keep = !r.rejected() && Boolean.TRUE.equals(r.matched());
                        break;
                    case FILTERED_OUT:
                        keep = !r.rejected() && Boolean.FALSE.equals(r.matched());
                        break;
                    case LATE:
                        keep = r.late();
                        break;
                    case REJECTED:
                        keep = r.rejected();
                        break;
                    default:
                        keep = true;
                }
                if (!keep) {
                    continue;
                }
                if (filter != null && filter.limit() > 0 && matchedCount >= filter.limit()) {
                    break;
                }
                out.add(r);
                matchedCount++;
            }
            return out;
        }
    }

    /** 结果状态枚举。 */
    public enum ResultStatus {
        ALL, MATCHED, FILTERED_OUT, LATE, REJECTED
    }

    /** 查询过滤条件（不可变）。 */
    public static final class ResultFilter {
        private final ResultStatus status;
        private final String ruleVersionId;
        private final int limit;

        private ResultFilter(ResultStatus status, String ruleVersionId, int limit) {
            this.status = status;
            this.ruleVersionId = ruleVersionId;
            this.limit = limit;
        }

        public static ResultFilter of(ResultStatus status) {
            return new ResultFilter(status, null, 0);
        }

        public static ResultFilter all() {
            return new ResultFilter(ResultStatus.ALL, null, 0);
        }

        public ResultFilter withVersion(String versionId) {
            return new ResultFilter(status, versionId, limit);
        }

        public ResultFilter withLimit(int limit) {
            return new ResultFilter(status, ruleVersionId, limit);
        }

        public ResultStatus status() {
            return status;
        }

        public String ruleVersionId() {
            return ruleVersionId;
        }

        public int limit() {
            return limit;
        }
    }
}
