package drvb.version;

/**
 * 基于事件时间的水位线：{@code watermark = maxObservedEventTime - allowedLateness}。
 *
 * <p>事件时间小于水位线即视为晚到事件（late），但<b>仍会被处理</b>——
 * 这正是"晚到事件使用对应历史版本"的入口。allowedLateness 表示系统愿意
 * 容忍的乱序程度。
 *
 * <p>初始无观测时水位线为 {@code Long.MIN_VALUE}（任何事件都不算晚到）。
 * 不可变值对象式更新：每次观测返回新水位线值。
 */
public final class Watermark {

    private final long allowedLateness;
    private long maxEventTime = Long.MIN_VALUE;

    public Watermark(long allowedLateness) {
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness 不能为负");
        }
        this.allowedLateness = allowedLateness;
    }

    /** 观测一条事件的时间，推进（绝不倒退）水位线，返回新水位线值。 */
    public long observe(long eventTime) {
        if (eventTime > maxEventTime) {
            maxEventTime = eventTime;
        }
        return current();
    }

    public long current() {
        if (maxEventTime == Long.MIN_VALUE) {
            return Long.MIN_VALUE;
        }
        return maxEventTime - allowedLateness;
    }

    public long allowedLateness() {
        return allowedLateness;
    }

    public long maxObservedEventTime() {
        return maxEventTime;
    }

    /** 给定事件时间是否晚于（小于）当前水位线。 */
    public boolean isLate(long eventTime) {
        return maxEventTime != Long.MIN_VALUE && eventTime < current();
    }
}
