package cep.time;

/**
 * 可注入的事件时间调度器，时间由外部显式推进（与墙钟、sleep 无关），
 * 因此流式计算在测试中完全确定。
 *
 * <p>同 deadline 的任务按注册顺序触发。
 */
public interface Scheduler {

    /** 注册一次性定时器，返回可用于取消的句柄。 */
    ScheduledTask schedule(long deadlineMillis, Runnable callback);

    /** 取消定时器（幂等）。 */
    void cancel(ScheduledTask task);

    /** 当前调度器时间。 */
    long currentTimeMillis();

    /** 直接把调度器时间设置为 t（不得回退）；主要供引擎统一驱动事件时间轴。 */
    default void setTime(long t) {
        throw new UnsupportedOperationException();
    }

    /** 最早未触发、未取消定时器的 deadline；无则 null。 */
    default Long peekDeadline() {
        throw new UnsupportedOperationException();
    }

    /**
     * 弹出且仅弹出最早一个 deadline &lt; upToExclusive 的任务回调（不推进逻辑时间）。
     * 引擎据此把定时器与缓冲事件按时间归并；无到期任务时返回 null。
     */
    default Runnable pollDue(long upToExclusive) {
        throw new UnsupportedOperationException();
    }

    /** 弹出最早一个 deadline ≤ upToInclusive 的任务回调（含 Long.MAX_VALUE 边界）。 */
    default Runnable pollDueInclusive(long upToInclusive) {
        return pollDue(upToInclusive == Long.MAX_VALUE ? Long.MAX_VALUE : upToInclusive + 1);
    }

    /** 清空全部定时器并把时间重置为最小值（供引擎 reset/会话重放）。 */
    default void reset() {
        throw new UnsupportedOperationException();
    }

    /** 推进到 newTimeMillis（不得回退），触发 deadline &lt; newTimeMillis 的全部任务，返回触发数量。 */
    default int advanceTime(long newTimeMillis) {
        throw new UnsupportedOperationException();
    }
}
