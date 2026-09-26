package dev.intervals.model;

import java.util.Objects;

/**
 * 一个连通区间 {@code [l,r] / [l,r) / (l,r] / (l,r)}，端点可为无穷。
 *
 * <p>不变量（构造时强制）：
 * <ul>
 *   <li>{@code lower <= upper}；{@code lower > upper} 为反向区间，直接拒绝；</li>
 *   <li>{@code lower == upper}（有限）时仅当两端均闭才合法，即单点区间 {@code [x,x]}；</li>
 *   <li>无穷端点的包含性恒为 {@link Edge#OPEN}（无穷处没有可包含的点，闭无穷归一化为开）。</li>
 * </ul>
 *
 * <p>不可变。
 */
public final class Interval {

    private final Endpoint lower;
    private final Edge leftEdge;
    private final Endpoint upper;
    private final Edge rightEdge;

    private Interval(Endpoint lower, Edge leftEdge, Endpoint upper, Edge rightEdge) {
        this.lower = lower;
        this.leftEdge = leftEdge;
        this.upper = upper;
        this.rightEdge = rightEdge;
    }

    /**
     * 创建区间。
     *
     * @throws IllegalArgumentException 反向区间、退化空区间（同端点但非双闭）
     */
    public static Interval of(Endpoint lower, Edge leftEdge, Endpoint upper, Edge rightEdge) {
        Objects.requireNonNull(lower, "lower");
        Objects.requireNonNull(upper, "upper");
        Objects.requireNonNull(leftEdge, "leftEdge");
        Objects.requireNonNull(rightEdge, "rightEdge");

        int cmp = lower.compareTo(upper);
        if (cmp > 0) {
            throw new IllegalArgumentException(
                    "反向区间被拒绝: lower " + lower + " > upper " + upper);
        }

        // 无穷端点强制为开
        Edge normLeft = lower.isInfinite() ? Edge.OPEN : leftEdge;
        Edge normRight = upper.isInfinite() ? Edge.OPEN : rightEdge;

        if (cmp == 0) {
            if (lower.isInfinite()) {
                throw new IllegalArgumentException(
                        "退化空区间被拒绝: 两端均为同一无穷 " + lower);
            }
            if (normLeft != Edge.CLOSED || normRight != Edge.CLOSED) {
                throw new IllegalArgumentException(
                        "退化空区间被拒绝: 端点相同 " + lower + " 时必须两端均闭（单点 [x,x]）");
            }
        }

        return new Interval(lower, normLeft, upper, normRight);
    }

    public static Interval singleton(Endpoint p) {
        return of(p, Edge.CLOSED, p, Edge.CLOSED);
    }

    public Endpoint lower() {
        return lower;
    }

    public Endpoint upper() {
        return upper;
    }

    public Edge leftEdge() {
        return leftEdge;
    }

    public Edge rightEdge() {
        return rightEdge;
    }

    public boolean isSingleton() {
        return lower.isFinite() && lower.compareTo(upper) == 0;
    }

    /** 是否包含给定点。 */
    public boolean contains(Endpoint p) {
        int cl = p.compareTo(lower);
        int cr = p.compareTo(upper);
        if (cl < 0 || cr > 0) {
            return false;
        }
        if (cl == 0 && lower.isFinite() && leftEdge != Edge.CLOSED) {
            return false;
        }
        if (cr == 0 && upper.isFinite() && rightEdge != Edge.CLOSED) {
            return false;
        }
        return true;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Interval other)) {
            return false;
        }
        return lower.equals(other.lower)
                && leftEdge == other.leftEdge
                && upper.equals(other.upper)
                && rightEdge == other.rightEdge;
    }

    @Override
    public int hashCode() {
        return Objects.hash(lower, leftEdge, upper, rightEdge);
    }

    @Override
    public String toString() {
        String l = (leftEdge == Edge.CLOSED ? "[" : "(") + lower;
        String r = upper + (rightEdge == Edge.CLOSED ? "]" : ")");
        return l + ", " + r;
    }
}
