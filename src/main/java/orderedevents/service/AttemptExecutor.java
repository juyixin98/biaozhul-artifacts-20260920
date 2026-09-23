package orderedevents.service;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import orderedevents.model.AttemptSpec;

/**
 * Runs one simulated attempt on a shared cached pool and enforces its deadline.
 *
 * <p>An attempt with behavior {@code timeout} keeps "working" (a long sleep)
 * until the framework cuts it off at the per-attempt timeout — this models a
 * downstream call that ignores cancellation. {@code fail} and {@code succeed}
 * settle themselves after their injected delay.
 *
 * <p>The attempt future is cancelled (thread interrupted) on either the
 * attempt timeout or an event-level cancellation arriving while the outer
 * worker is blocked awaiting it.
 */
final class AttemptExecutor {

    /** How long a "timeout-behavior" attempt pretends to work before the deadline kills it. */
    private static final long HANG_MILLIS = TimeUnit.MINUTES.toMillis(30);

    sealed interface Outcome permits Outcome.Succeeded, Outcome.Failed, Outcome.TimedOut, Outcome.Cancelled {
        record Succeeded(Object value) implements Outcome {
        }

        record Failed(String error) implements Outcome {
        }

        record TimedOut(String error) implements Outcome {
        }

        record Cancelled() implements Outcome {
        }
    }

    private final ExecutorService pool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "attempt-work");
        t.setDaemon(true);
        return t;
    });

    /**
     * Runs one attempt, cutting it off at {@code timeoutMillis}. A deadline
     * cut-off reports TIMED_OUT; an interrupt while waiting (event cancel or
     * shutdown) reports CANCELLED.
     */
    Outcome run(AttemptSpec attempt, long timeoutMillis, Object payload) {
        Future<Outcome> f = pool.submit(() -> {
            long sleep = AttemptSpec.TIMEOUT.equals(attempt.behavior()) ? HANG_MILLIS : attempt.delayMillis();
            if (sleep > 0) {
                Thread.sleep(sleep);
            }
            return switch (attempt.behavior()) {
                case AttemptSpec.SUCCEED -> new Outcome.Succeeded(payload);
                case AttemptSpec.FAIL ->
                        new Outcome.Failed(attempt.error() != null ? attempt.error() : "injected failure");
                // Only reachable if a timeout-behavior attempt somehow wakes under the deadline.
                case AttemptSpec.TIMEOUT ->
                        new Outcome.TimedOut(attempt.error() != null ? attempt.error() : "attempt timed out");
                default -> throw new IllegalStateException(attempt.behavior());
            };
        });

        try {
            return f.get(Math.max(1, timeoutMillis), TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            f.cancel(true);
            String msg = attempt.error() != null ? attempt.error()
                    : "attempt timed out after " + timeoutMillis + "ms";
            return new Outcome.TimedOut(msg);
        } catch (InterruptedException e) {
            // Event cancellation (or shutdown) while waiting for the attempt.
            f.cancel(true);
            Thread.currentThread().interrupt();
            return new Outcome.Cancelled();
        } catch (Exception e) {
            f.cancel(true);
            return new Outcome.Failed("attempt executor error: " + rootMessage(e));
        }
    }

    void shutdown() {
        pool.shutdownNow();
    }

    private static String rootMessage(Throwable t) {
        Throwable c = t;
        while (c.getCause() != null && c.getCause() != c) {
            c = c.getCause();
        }
        return String.valueOf(c);
    }
}
