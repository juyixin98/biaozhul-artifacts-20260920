package streammatch.time;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * 确定性手动调度器：任务只在显式 {@link #advanceTime(long)} 时触发，便于测试中精确控制
 * “超时先到还是事件先到”。同一触发时刻的任务按注册顺序（handle 递增）执行。
 */
public final class ManualScheduler implements TaskScheduler {

    private record Scheduled(long handle, long fireAt, Runnable task) {
    }

    private final PriorityQueue<Scheduled> queue =
            new PriorityQueue<>(Comparator.comparingLong((Scheduled s) -> s.fireAt)
                    .thenComparingLong(s -> s.handle));
    private long nextHandle = 1;
    private long currentTime = Long.MIN_VALUE;

    @Override
    public long scheduleAt(long fireAtMillis, Runnable task) {
        long handle = nextHandle++;
        queue.add(new Scheduled(handle, fireAtMillis, task));
        return handle;
    }

    @Override
    public void cancel(long handle) {
        queue.removeIf(s -> s.handle == handle);
    }

    /** 当前逻辑时间（未推进过时为 {@link Long#MIN_VALUE}）。 */
    public long now() {
        return currentTime;
    }

    /** 是否还有未触发任务。 */
    public boolean hasPending() {
        return !queue.isEmpty();
    }

    /**
     * 推进到 {@code targetTime}：触发所有 fireAt &lt;= targetTime 的任务（按时间、注册序）。
     * 任务执行过程中新调度的任务只要时刻满足条件也会在本轮触发。
     */
    public void advanceTime(long targetTime) {
        if (targetTime < currentTime) {
            throw new IllegalArgumentException("scheduler cannot move backwards");
        }
        currentTime = targetTime;
        List<Runnable> due = new ArrayList<>();
        while (!queue.isEmpty() && queue.peek().fireAt <= targetTime) {
            due.add(queue.poll().task());
        }
        // 先全部摘出再执行：执行中新加入的任务进入下一轮判断，保证注册序在同刻的确定性
        for (Runnable r : due) {
            r.run();
        }
        if (!queue.isEmpty() && queue.peek().fireAt <= targetTime) {
            advanceTime(targetTime);
        }
    }
}
