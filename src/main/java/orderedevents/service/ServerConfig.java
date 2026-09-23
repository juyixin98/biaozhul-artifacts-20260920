package orderedevents.service;

/**
 * Server-wide tuning. Every limit is per partition.
 *
 * @param port                    HTTP listen port (0 = ephemeral, used by tests)
 * @param maxInFlight             buffer cap: total events admitted per partition
 *                                (running + buffered), submissions beyond it are rejected
 * @param maxConcurrent           max attempts executing simultaneously per partition
 * @param defaultMaxAttempts      default retry budget (total attempts) per event
 * @param maxAttemptsCap          hard cap a client may request
 * @param defaultTimeoutMillis    default per-attempt timeout
 * @param maxTimeoutMillis        hard cap a client may request for a timeout
 * @param retryBackoffMillis      fixed pause between retries
 * @param longPollCapMillis       maximum wait a GET results request may ask for
 */
public record ServerConfig(
        int port,
        int maxInFlight,
        int maxConcurrent,
        int defaultMaxAttempts,
        int maxAttemptsCap,
        long defaultTimeoutMillis,
        long maxTimeoutMillis,
        long retryBackoffMillis,
        long longPollCapMillis
) {

    public static ServerConfig defaults(int port) {
        return new ServerConfig(port, 8, 2, 3, 10, 1_000, 60_000, 50, 30_000);
    }
}
