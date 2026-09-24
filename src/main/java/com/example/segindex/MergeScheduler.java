package com.example.segindex;

import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Daemon background thread that merges all live segments into one whenever
 * the segment count reaches the configured merge factor. Merge failures
 * (including simulated crashes) are recorded and the thread keeps running.
 */
final class MergeScheduler {

    private final Index index;
    private final ReentrantLock lock = new ReentrantLock();
    private final Condition wake = lock.newCondition();
    private volatile boolean running;
    private volatile Throwable lastError;
    private Thread thread;

    MergeScheduler(Index index) {
        this.index = index;
    }

    void start() {
        running = true;
        thread = new Thread(this::loop, "segindex-merger");
        thread.setDaemon(true);
        thread.start();
    }

    void signal() {
        lock.lock();
        try {
            wake.signal();
        } finally {
            lock.unlock();
        }
    }

    Throwable lastError() {
        return lastError;
    }

    private void loop() {
        while (running) {
            waitForSignal();
            if (!running) {
                return;
            }
            if (index.liveSegmentCount() >= index.config().mergeFactor()) {
                try {
                    index.forceMerge();
                } catch (Throwable t) {
                    lastError = t;
                }
            }
        }
    }

    private void waitForSignal() {
        lock.lock();
        try {
            wake.await(1, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            lock.unlock();
        }
    }

    void shutdown() {
        running = false;
        signal();
        if (thread != null) {
            try {
                thread.join(2000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /** Test helper: wait until at most {@code maxSegments} remain or timeout. */
    boolean awaitMerged(int maxSegments, long timeoutMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMillis;
        while (System.currentTimeMillis() < deadline) {
            if (index.liveSegmentCount() <= maxSegments) {
                return true;
            }
            List<String> segs = index.manifestSnapshot().segments;
            if (segs.size() >= index.config().mergeFactor()) {
                signal();
            }
            Thread.sleep(20);
        }
        return index.liveSegmentCount() <= maxSegments;
    }
}
