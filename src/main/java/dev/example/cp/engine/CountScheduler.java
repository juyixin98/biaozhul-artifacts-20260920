package dev.example.cp.engine;

/**
 * 每处理 {@code count} 条事件触发一次检查点（固定条数调度）。
 */
public final class CountScheduler implements CheckpointScheduler {

    private final long count;

    public CountScheduler(long count) {
        if (count <= 0) {
            throw new IllegalArgumentException("count must be positive");
        }
        this.count = count;
    }

    @Override
    public boolean shouldCheckpointAfterEvent(long processedInRun, Clock clock) {
        // 引擎传入的是“全局已消费偏移”（跨恢复稳定），因此检查点边界不随进程重启漂移。
        return processedInRun > 0 && (processedInRun + 1) % count == 0;
    }
}
