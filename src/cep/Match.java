package cep;

import java.util.Objects;

/**
 * 一次完整匹配：某实体下按时间先后（含输入序号破并）出现的 A、B、C 三个事件，
 * 且首事件 A 与末事件 C 的时间差不超过 {@link Engine#WINDOW_MS}（10 秒）。
 *
 * 对象不可变。
 */
public final class Match {

    public final Event a;
    public final Event b;
    public final Event c;

    public Match(Event a, Event b, Event c) {
        this.a = a;
        this.b = b;
        this.c = c;
    }

    public String entityId() {
        return a.entityId;
    }

    public boolean withinWindow(long windowMs) {
        return c.timestamp - a.timestamp <= windowMs;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Match)) return false;
        Match match = (Match) o;
        return Objects.equals(a, match.a)
                && Objects.equals(b, match.b)
                && Objects.equals(c, match.c);
    }

    @Override
    public int hashCode() {
        return Objects.hash(a, b, c);
    }

    @Override
    public String toString() {
        return "Match{A#" + a.seq + "(ts=" + a.timestamp + ") -> B#"
                + b.seq + "(ts=" + b.timestamp + ") -> C#"
                + c.seq + "(ts=" + c.timestamp + "), entity=" + a.entityId + '}';
    }
}
