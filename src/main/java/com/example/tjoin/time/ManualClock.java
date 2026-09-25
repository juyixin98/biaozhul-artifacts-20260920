package com.example.tjoin.time;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * Deterministic, externally driven clock for tests. Time stands still until
 * {@link #advanceTo(long)} or {@link #advanceBy(long)} is called; any
 * scheduled tasks whose time has come are executed synchronously (in
 * timestamp order, ties in registration order).
 */
public final class ManualClock implements Clock, Scheduler {

    /** Scheduled timer. */
    private static final class Timer {
        final long atMillis;
        final long seq;
        final Runnable action;

        Timer(long atMillis, long seq, Runnable action) {
            this.atMillis = atMillis;
            this.seq = seq;
            this.action = action;
        }
    }

    private long now;
    private long seqCounter;
    private final PriorityQueue<Timer> timers = new PriorityQueue<>(
            Comparator.comparingLong((Timer t) -> t.atMillis).thenComparingLong(t -> t.seq));

    public ManualClock() {
        this(0L);
    }

    public ManualClock(long startMillis) {
        this.now = startMillis;
    }

    @Override
    public long currentTimeMillis() {
        return now;
    }

    /**
     * Move time forward to {@code targetMillis}, firing every due timer.
     * Never moves backwards. Timers scheduled <em>during</em> a firing that
     * are themselves due at/before {@code targetMillis} also fire; an action
     * that perpetually reschedules itself at the same timestamp will
     * therefore keep firing (by design — callers must advance past it).
     *
     * @return number of timers fired
     */
    public int advanceTo(long targetMillis) {
        if (targetMillis < now) {
            throw new IllegalArgumentException(
                    "ManualClock cannot move backwards: " + targetMillis + " < " + now);
        }
        int fired = 0;
        // Fire in batches so that timers scheduled during a fire are also
        // run when due.
        while (true) {
            List<Timer> due = new ArrayList<>();
            while (!timers.isEmpty() && timers.peek().atMillis <= targetMillis) {
                due.add(timers.poll());
            }
            if (due.isEmpty()) {
                break;
            }
            // Poll order is already timestamp/seq order, but a timer can
            // reschedule an earlier-ts timer — guard with per-batch advance.
            now = Math.max(now, due.get(due.size() - 1).atMillis);
            for (Timer t : due) {
                now = Math.max(now, t.atMillis);
                t.action.run();
                fired++;
            }
        }
        now = targetMillis;
        return fired;
    }

    public int advanceBy(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta must be >= 0");
        }
        return advanceTo(now + deltaMillis);
    }

    @Override
    public void scheduleAt(long atMillis, Runnable action) {
        if (atMillis < now) {
            // Timer already due: run immediately on advance semantics —
            // enqueue at 'now' so it runs in order.
            atMillis = now;
        }
        timers.add(new Timer(atMillis, seqCounter++, action));
    }

    @Override
    public void scheduleAfter(long delayMillis, Runnable action) {
        if (delayMillis < 0) {
            throw new IllegalArgumentException("delay must be >= 0");
        }
        scheduleAt(now + delayMillis, action);
    }

    /** Number of pending (not yet fired) timers. */
    public int pendingTimers() {
        return timers.size();
    }
}
