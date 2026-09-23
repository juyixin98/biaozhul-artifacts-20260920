package com.tjoin.time;

import java.util.Comparator;
import java.util.PriorityQueue;

/**
 * 手动处理时间服务：不使用任何真实线程。任务在虚拟时间轴上排队，
 * 仅当 {@link #advanceBy(long)} / {@link #advanceTo(long)} 推进时钟时才触发。
 *
 * <p>周期任务在时钟推进过程中按到期时刻依次触发（可能触发多次），
 * 触发顺序与真实调度一致：同一时刻的任务先到期先执行，随后重新排队到下一周期。
 */
public final class ManualProcessingTimeService implements ProcessingTimeService {

    private final ManualClock clock;
    private final PriorityQueue<Timer> timers = new PriorityQueue<>(
            Comparator.comparingLong((Timer t) -> t.period >= 0 ? t.nextExecution : t.executionTime)
                    .thenComparingLong(t -> t.sequence));
    private long seq = 0;

    public ManualProcessingTimeService() {
        this(0L);
    }

    public ManualProcessingTimeService(long startTime) {
        this.clock = new ManualClock(startTime);
    }

    @Override
    public Clock clock() {
        return clock;
    }

    /** 当前等待中的定时器数量（不含已取消）。 */
    public int pendingCount() {
        timers.removeIf(t -> t.cancelled);
        return timers.size();
    }

    /** 下一个定时器的触发时刻；无任务时返回 null。 */
    public Long nextTriggerTime() {
        timers.removeIf(t -> t.cancelled);
        Timer t = timers.peek();
        return t == null ? null : t.when();
    }

    @Override
    public ScheduledTask scheduleOnce(long delayMillis, Runnable task) {
        Timer t = new Timer(clock.now() + Math.max(0, delayMillis), -1, task, seq++);
        timers.add(t);
        return t;
    }

    @Override
    public ScheduledTask scheduleAtFixedRate(long initialDelayMillis, long periodMillis, Runnable task) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("period must be positive");
        }
        Timer t = new Timer(clock.now() + Math.max(0, initialDelayMillis), periodMillis, task, seq++);
        timers.add(t);
        return t;
    }

    /** 推进虚拟时间并触发所有到期任务。返回触发的任务次数。 */
    public int advanceBy(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("time cannot move backwards");
        }
        return advanceTo(clock.now() + deltaMillis);
    }

    /** 将虚拟时间推进到指定绝对时刻，触发沿途所有到期任务。 */
    public int advanceTo(long targetTime) {
        if (targetTime < clock.now()) {
            throw new IllegalArgumentException("time cannot move backwards");
        }
        int fired = 0;
        while (true) {
            timers.removeIf(t -> t.cancelled);
            Timer next = timers.peek();
            if (next == null || next.when() > targetTime) {
                break;
            }
            timers.poll();
            // 先把时钟移动到任务时刻（任务内读到的时间是其到期时刻）
            clock.setTime(next.when());
            next.task.run();
            fired++;
            if (next.period >= 0 && !next.cancelled) {
                // 更新键后必须重新入堆，PriorityQueue 不会感知已有元素的键变化
                next.nextExecution = next.when() + next.period;
                timers.add(next);
            }
        }
        clock.setTime(targetTime);
        return fired;
    }

    private final class Timer implements ScheduledTask {
        long executionTime;
        long nextExecution;
        final long period; // <0 表示一次性
        final Runnable task;
        final long sequence;
        volatile boolean cancelled;

        Timer(long executionTime, long period, Runnable task, long sequence) {
            this.executionTime = executionTime;
            this.nextExecution = executionTime;
            this.period = period;
            this.task = task;
            this.sequence = sequence;
        }

        long when() {
            return period >= 0 ? nextExecution : executionTime;
        }

        @Override
        public boolean cancel() {
            boolean was = !cancelled;
            cancelled = true;
            return was;
        }

        @Override
        public boolean isCancelled() {
            return cancelled;
        }
    }
}
