package com.example.cptx.core;

import java.util.Map;

/**
 * 检查点触发策略（屏障调度），可注入。
 * Pipeline 每处理完一条事件回调一次；返回 true 即在当前偏移处插入屏障并执行检查点。
 */
public interface CheckpointPolicy {

    /**
     * @param eventsSinceLastCheckpoint 自上一次检查点以来处理的事件条数（≥1）
     * @param totalProcessed            累计已处理条数（等于 nextOffset）
     * @param clock                     可注入时钟
     */
    boolean shouldCheckpoint(long eventsSinceLastCheckpoint, long totalProcessed, Clock clock);

    /** 一次检查点完成后回调，用于重置内部计时/计数。 */
    default void onCheckpoint(Clock clock) {}

    /** 按固定事件条数触发。 */
    static CheckpointPolicy count(long every) {
        if (every <= 0) throw new IllegalArgumentException("every 必须为正数");
        return new CheckpointPolicy() {
            @Override
            public boolean shouldCheckpoint(long since, long total, Clock clock) {
                return since >= every;
            }

            @Override
            public String toString() {
                return "CountPolicy(every=" + every + ")";
            }
        };
    }

    /** 按注入时钟的墙上时间间隔触发。 */
    static CheckpointPolicy wallTime(Clock clock, long intervalMillis) {
        if (intervalMillis <= 0) throw new IllegalArgumentException("intervalMillis 必须为正数");
        long[] last = {clock.nowMillis()};
        return new CheckpointPolicy() {
            @Override
            public boolean shouldCheckpoint(long since, long total, Clock c) {
                return c.nowMillis() - last[0] >= intervalMillis;
            }

            @Override
            public void onCheckpoint(Clock c) {
                last[0] = c.nowMillis();
            }

            @Override
            public String toString() {
                return "TimePolicy(intervalMillis=" + intervalMillis + ")";
            }
        };
    }

    /** 手动策略：trip() 后下一条事件处理完即触发；也可直接调用 Pipeline.flushCheckpoint()。 */
    static Manual manual() {
        return new Manual();
    }

    final class Manual implements CheckpointPolicy {
        private boolean requested;

        public synchronized void trip() {
            requested = true;
        }

        @Override
        public synchronized boolean shouldCheckpoint(long since, long total, Clock clock) {
            if (requested) {
                requested = false;
                return true;
            }
            return false;
        }

        @Override
        public synchronized void onCheckpoint(Clock clock) {
            requested = false;
        }
    }
}
