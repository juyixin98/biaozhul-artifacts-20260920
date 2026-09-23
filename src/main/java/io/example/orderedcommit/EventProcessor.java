package io.example.orderedcommit;

/**
 * Pluggable processing logic for an event.
 *
 * <p>The processor simulates (or performs) the asynchronous work. It runs on a
 * worker thread and is expected to honour thread interruption: the service
 * interrupts an in-flight attempt when the event is cancelled or its attempt
 * timeout fires. Throwing any exception marks the attempt failed; the service
 * decides whether to retry from {@link Event#maxAttempts()}.
 */
@FunctionalInterface
public interface EventProcessor {
    Object process(Event event) throws Exception;
}
