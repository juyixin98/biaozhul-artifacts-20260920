package intervaljoin;

import java.util.Objects;

/** 一次成功连接的输出：一对左右事件。 */
public final class Pair {
    public final Event left;
    public final Event right;

    public Pair(Event left, Event right) {
        this.left = left;
        this.right = right;
    }

    /** 用于集合比对：同一 (leftId, rightId) 只算一次。 */
    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Pair)) return false;
        Pair p = (Pair) o;
        return left.id.equals(p.left.id) && right.id.equals(p.right.id);
    }

    @Override
    public int hashCode() {
        return Objects.hash(left.id, right.id);
    }

    @Override
    public String toString() {
        return "[" + left.id + " <-> " + right.id + " key=" + left.key
                + " lts=" + left.ts + " rts=" + right.ts + "]";
    }
}
