package dev.example.cp.engine;

/** 永不自动触发（只能通过显式屏障触发，用于 HTTP 服务与精确测试）。 */
public final class ManualScheduler implements CheckpointScheduler {

    @Override
    public boolean shouldCheckpointAfterEvent(long processedInRun, Clock clock) {
        return false;
    }
}
