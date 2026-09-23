package io.example.orderedcommit;

/**
 * Default in-memory processor used by the standalone service.
 *
 * <p>It injects configurable latency and failure so the ordering, timeout and
 * retry semantics can be exercised end to end:
 * <ul>
 *   <li>each attempt sleeps for {@link Event#delayMillis()};</li>
 *   <li>if the event was submitted with {@code fail=true} every attempt
 *       throws, eventually exhausting the retry budget;</li>
 *   <li>sleeps are interruptible, so attempt timeouts and cancellations wake
 *       the attempt immediately instead of waiting out the full delay.</li>
 * </ul>
 */
public class SleepingEventProcessor implements EventProcessor {

    @Override
    public Object process(Event event) throws Exception {
        long start = System.currentTimeMillis();
        long remaining = event.delayMillis;
        while (remaining > 0) {
            try {
                Thread.sleep(remaining);
                break;
            } catch (InterruptedException e) {
                long elapsed = System.currentTimeMillis() - start;
                remaining = event.delayMillis - elapsed;
                if (event.timedOut) {
                    throw new Exception("attempt timed out after " + event.attemptTimeoutMillis + "ms", e);
                }
                if (Thread.currentThread().isInterrupted()) {
                    throw e;
                }
                // spurious wakeup: keep sleeping
            }
        }
        if (event.fail) {
            throw new Exception("injected processing failure on attempt " + event.attempts);
        }
        return java.util.Map.of(
                "handled", true,
                "attempt", event.attempts,
                "delayMillis", event.delayMillis);
    }
}
