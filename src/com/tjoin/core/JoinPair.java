package com.tjoin.core;

import java.util.Objects;

/**
 * 一次区间连接输出：一对事件（一个左事件 + 一个右事件）。
 *
 * <p>相等性基于两个事件 ID（与具体值无关），用于断言“每对事件仅输出一次”。
 */
public final class JoinPair {

    private final Event left;
    private final Event right;

    public JoinPair(Event left, Event right) {
        if (left.side() != StreamSide.LEFT || right.side() != StreamSide.RIGHT) {
            throw new IllegalArgumentException("JoinPair requires (LEFT, RIGHT) event ordering");
        }
        this.left = Objects.requireNonNull(left);
        this.right = Objects.requireNonNull(right);
    }

    public Event left() {
        return left;
    }

    public Event right() {
        return right;
    }

    /** 连接键（两侧必须相同才会产生配对）。 */
    public String key() {
        return left.key();
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof JoinPair)) return false;
        JoinPair other = (JoinPair) o;
        return left.id().equals(other.left.id()) && right.id().equals(other.right.id());
    }

    @Override
    public int hashCode() {
        return Objects.hash(left.id(), right.id());
    }

    @Override
    public String toString() {
        return "Pair[" + left.id() + "(" + left.timestamp() + ")"
                + " <-> " + right.id() + "(" + right.timestamp() + ")"
                + ", key=" + key() + "]";
    }
}
