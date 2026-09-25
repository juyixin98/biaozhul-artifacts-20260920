package drvb.model;

/**
 * 单条事件经流处理后的精确结果（参考实现逐条产出，不做近似聚合）。
 *
 * <p>要么被规则过滤（{@code matched=true/false}，并记录实际生效的不可变版本），
 * 要么被拒绝（{@code rejected=true}，携带 {@link RejectReason}）。
 * 另外记录事件时间、到达时的处理时间、水位线以及该事件是否为晚到事件，
 * 便于测试断言边界行为。
 */
public final class ProcessResult {

    private final String eventId;
    private final long eventTime;
    private final long processingTime;
    private final long watermarkBefore;
    private final long watermarkAfter;
    private final boolean late;

    private final boolean rejected;
    private final RejectReason rejectReason;
    private final String detail;

    private final String ruleVersionId;
    private final long bindingSeq;
    private final Boolean matched;

    private ProcessResult(Builder b) {
        this.eventId = b.eventId;
        this.eventTime = b.eventTime;
        this.processingTime = b.processingTime;
        this.watermarkBefore = b.watermarkBefore;
        this.watermarkAfter = b.watermarkAfter;
        this.late = b.late;
        this.rejected = b.rejected;
        this.rejectReason = b.rejectReason;
        this.detail = b.detail;
        this.ruleVersionId = b.ruleVersionId;
        this.bindingSeq = b.bindingSeq;
        this.matched = b.matched;
    }

    public static Builder builder(Event e) {
        return new Builder(e.eventId(), e.eventTime());
    }

    public String eventId() {
        return eventId;
    }

    public long eventTime() {
        return eventTime;
    }

    public long processingTime() {
        return processingTime;
    }

    public long watermarkBefore() {
        return watermarkBefore;
    }

    public long watermarkAfter() {
        return watermarkAfter;
    }

    public boolean late() {
        return late;
    }

    public boolean rejected() {
        return rejected;
    }

    public RejectReason rejectReason() {
        return rejectReason;
    }

    public String detail() {
        return detail;
    }

    /** 实际生效的不可变规则版本 id（被拒绝时为 null）。 */
    public String ruleVersionId() {
        return ruleVersionId;
    }

    /** 命中的绑定序号（被拒绝时为 0）。 */
    public long bindingSeq() {
        return bindingSeq;
    }

    /** 过滤结果（被拒绝时为 null）。 */
    public Boolean matched() {
        return matched;
    }

    public static final class Builder {
        private final String eventId;
        private final long eventTime;
        private long processingTime;
        private long watermarkBefore;
        private long watermarkAfter;
        private boolean late;
        private boolean rejected;
        private RejectReason rejectReason;
        private String detail;
        private String ruleVersionId;
        private long bindingSeq;
        private Boolean matched;

        Builder(String eventId, long eventTime) {
            this.eventId = eventId;
            this.eventTime = eventTime;
        }

        public Builder processingTime(long t) {
            this.processingTime = t;
            return this;
        }

        public Builder watermarkBefore(long w) {
            this.watermarkBefore = w;
            return this;
        }

        public Builder watermarkAfter(long w) {
            this.watermarkAfter = w;
            return this;
        }

        public Builder late(boolean v) {
            this.late = v;
            return this;
        }

        public Builder rejected(RejectReason reason, String detail) {
            this.rejected = true;
            this.rejectReason = reason;
            this.detail = detail;
            return this;
        }

        public Builder filtered(String versionId, long bindingSeq, boolean matched) {
            this.rejected = false;
            this.ruleVersionId = versionId;
            this.bindingSeq = bindingSeq;
            this.matched = matched;
            return this;
        }

        public ProcessResult build() {
            return new ProcessResult(this);
        }
    }
}
