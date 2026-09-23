package orderedevents.model;

import java.util.List;

/**
 * Client-supplied specification of an event to process.
 *
 * @param payload      opaque JSON value echoed into the success result
 * @param attempts     per-attempt simulation plan (delay/behavior/error)
 * @param timeoutMillis per-attempt timeout; null means server default
 * @param maxAttempts  total attempts for this event (1 = no retries); null means server default
 */
public record EventSpec(Object payload, List<AttemptSpec> attempts, Long timeoutMillis, Integer maxAttempts) {
}
