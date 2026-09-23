package topk;

import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

/**
 * One data row: a group key, a numeric sort value, and a globally unique
 * sequence number assigned at load time. The sequence number makes the
 * ordering total: ties on value are broken by seq, so the result of any
 * TopK computation is fully deterministic regardless of sharding or merge
 * order.
 */
public final class Row implements Comparable<Row> {

    /** Ranking order: value DESC, then seq ASC (earlier-loaded row wins ties). */
    public static final Comparator<Row> RANK_ORDER =
            Comparator.comparingLong(Row::value).reversed()
                      .thenComparingLong(Row::seq);

    private final String group;
    private final long value;
    private final long seq;

    public Row(String group, long value, long seq) {
        this.group = Objects.requireNonNull(group, "group");
        this.value = value;
        this.seq = seq;
    }

    public String group() { return group; }
    public long value() { return value; }
    public long seq() { return seq; }

    @Override
    public int compareTo(Row other) {
        return RANK_ORDER.compare(this, other);
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("group", group);
        m.put("value", value);
        m.put("seq", seq);
        return m;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Row)) return false;
        Row r = (Row) o;
        return value == r.value && seq == r.seq && group.equals(r.group);
    }

    @Override
    public int hashCode() {
        return Objects.hash(group, value, seq);
    }

    @Override
    public String toString() {
        return "Row{group=" + group + ", value=" + value + ", seq=" + seq + "}";
    }
}
