package drvb.version;

/**
 * 一条"生效绑定"：声明某规则版本从事件时间 {@code effectiveFrom} 起生效，
 * 直到下一条绑定的 effectiveFrom（左闭右开区间 [from, nextFrom)）。
 *
 * <p>绑定本身同样不可变；序号 {@code seq} 单调递增，标识发布/回滚操作的先后。
 * 回滚不是删除绑定，而是新增一条指向旧版本的新绑定。
 */
public final class Binding {

    private final long seq;
    private final long effectiveFrom;
    private final String ruleVersionId;
    private final long publishedAt;
    private final String operation;
    private final String note;

    public Binding(long seq, long effectiveFrom, String ruleVersionId,
                   long publishedAt, String operation, String note) {
        this.seq = seq;
        this.effectiveFrom = effectiveFrom;
        this.ruleVersionId = ruleVersionId;
        this.publishedAt = publishedAt;
        this.operation = operation;
        this.note = note;
    }

    public long seq() {
        return seq;
    }

    public long effectiveFrom() {
        return effectiveFrom;
    }

    public String ruleVersionId() {
        return ruleVersionId;
    }

    public long publishedAt() {
        return publishedAt;
    }

    /** "PUBLISH"（新版本首发）或 "ROLLBACK"（指向已存在版本）。 */
    public String operation() {
        return operation;
    }

    public String note() {
        return note;
    }
}
