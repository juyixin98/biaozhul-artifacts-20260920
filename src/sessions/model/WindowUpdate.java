package sessions.model;

import java.util.Objects;

/**
 * 结果流上的一条明确变更记录（changelog）。
 *
 * <p>算子从不就地修改已发出的结果：每次窗口封窗发出 {@link ResultKind#ADD}；
 * 迟到事件导致已封窗被并入更大窗口时，先对旧窗口发 RETRACT，再对新窗口发 ADD。
 */
public final class WindowUpdate {
    private final long sequence;
    private final ResultKind kind;
    private final String key;
    private final Window window;
    private final long aggregate;
    /** 发出这条变更时的水位线（便于审计）。 */
    private final long emittedAtWatermark;

    public WindowUpdate(long sequence, ResultKind kind, String key, Window window,
                        long aggregate, long emittedAtWatermark) {
        this.sequence = sequence;
        this.kind = kind;
        this.key = key;
        this.window = window;
        this.aggregate = aggregate;
        this.emittedAtWatermark = emittedAtWatermark;
    }

    public long sequence() {
        return sequence;
    }

    public ResultKind kind() {
        return kind;
    }

    public String key() {
        return key;
    }

    public Window window() {
        return window;
    }

    public long aggregate() {
        return aggregate;
    }

    public long emittedAtWatermark() {
        return emittedAtWatermark;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof WindowUpdate u)) {
            return false;
        }
        return sequence == u.sequence && kind == u.kind && aggregate == u.aggregate
                && emittedAtWatermark == u.emittedAtWatermark
                && key.equals(u.key) && window.equals(u.window);
    }

    @Override
    public int hashCode() {
        return Objects.hash(sequence, kind, key, window, aggregate, emittedAtWatermark);
    }

    @Override
    public String toString() {
        return "#" + sequence + " " + kind + " key=" + key + " window=" + window
                + " agg=" + aggregate + " @W=" + emittedAtWatermark;
    }
}
