package cep.time;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 基于最小堆的事件时间调度器（与墙钟无关，时间完全由 {@link #setTime}/{@link #advanceTime}
 * 注入），因此测试中结果完全确定。
 *
 * <p>同 deadline 的任务按注册顺序触发；引擎通过 {@link #pollDue(long)} /
 * {@link #pollDueInclusive(long)} 只弹出到期的最早任务，从而能把"定时器触发"与
 * "到点的缓冲事件"放在同一条事件时间轴上归并。
 */
public final class HeapScheduler implements Scheduler {

    private static final class Task implements ScheduledTask {
        final long seq;
        final long deadline;
        final Runnable callback;
        volatile boolean cancelled;

        Task(long seq, long deadline, Runnable callback) {
            this.seq = seq;
            this.deadline = deadline;
            this.callback = callback;
        }

        @Override public long deadlineMillis() { return deadline; }
        @Override public boolean isCancelled() { return cancelled; }
        @Override public void cancel() { cancelled = true; }
    }

    private final PriorityQueue<Task> heap =
            new PriorityQueue<>(Comparator.comparingLong((Task t) -> t.deadline)
                    .thenComparingLong(t -> t.seq));
    private final AtomicLong sequence = new AtomicLong();
    private long now;

    public HeapScheduler() { this(Long.MIN_VALUE); }

    public HeapScheduler(long startTime) { this.now = startTime; }

    @Override
    public ScheduledTask schedule(long deadlineMillis, Runnable callback) {
        Task t = new Task(sequence.getAndIncrement(), deadlineMillis, callback);
        heap.add(t);
        return t;
    }

    @Override
    public void cancel(ScheduledTask task) {
        task.cancel();
        if (task instanceof Task t && heap.peek() == t) {
            purgeCancelled();
        }
    }

    @Override public long currentTimeMillis() { return now; }

    @Override
    public void setTime(long t) {
        if (t < now) {
            throw new IllegalArgumentException("时间不能回退: " + t + " < " + now);
        }
        now = t;
    }

    @Override
    public Long peekDeadline() {
        purgeCancelled();
        Task t = heap.peek();
        return t == null ? null : t.deadline;
    }

    @Override
    public Runnable pollDue(long upToExclusive) {
        purgeCancelled();
        Task t = heap.peek();
        if (t == null || t.deadline >= upToExclusive) {
            return null;
        }
        heap.poll();
        return t.callback;
    }

    /** 弹出最早一个 deadline ≤ upToInclusive 的任务回调（含 deadline = Long.MAX_VALUE）。 */
    public Runnable pollDueInclusive(long upToInclusive) {
        purgeCancelled();
        Task t = heap.peek();
        if (t == null || t.deadline > upToInclusive) {
            return null;
        }
        heap.poll();
        return t.callback;
    }

    @Override
    public int advanceTime(long newTimeMillis) {
        if (newTimeMillis < now) {
            throw new IllegalArgumentException("时间不能回退: " + newTimeMillis + " < " + now);
        }
        List<Runnable> due = new ArrayList<>();
        Runnable cb;
        while ((cb = pollDue(newTimeMillis)) != null) {
            due.add(cb);
        }
        now = newTimeMillis;
        int fired = 0;
        for (Runnable r : due) {
            r.run();
            fired++;
        }
        return fired;
    }

    @Override
    public void reset() {
        heap.clear();
        now = Long.MIN_VALUE;
    }

    private void purgeCancelled() {
        Task t;
        while ((t = heap.peek()) != null && t.cancelled) {
            heap.poll();
        }
    }
}
