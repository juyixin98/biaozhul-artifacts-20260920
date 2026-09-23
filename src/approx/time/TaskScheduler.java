package approx.time;

/**
 * Injectable periodic task scheduler.
 *
 * <p>Scheduling is deliberately minimal: the library only needs "do X every N
 * milliseconds". Production wiring ({@link SystemRuntime}) uses a real thread;
 * test wiring ({@link ManualEnvironment}) runs the tasks only when the test
 * advances a virtual clock, so window rotation is fully deterministic.
 */
public interface TaskScheduler extends AutoCloseable {
    /**
     * Run {@code task} periodically.
     *
     * @param initialDelayMillis time until the first execution
     * @param periodMillis interval between subsequent executions
     * @return a cancellation handle
     */
    CancellationToken schedulePeriodic(long initialDelayMillis, long periodMillis, Runnable task);

    /** Backward-compatible variant: first execution after one period. */
    default CancellationToken schedulePeriodic(long periodMillis, Runnable task) {
        return schedulePeriodic(periodMillis, periodMillis, task);
    }

    @Override
    void close();

    /** Handle used to cancel a periodic task. */
    @FunctionalInterface
    interface CancellationToken {
        void cancel();
    }
}
