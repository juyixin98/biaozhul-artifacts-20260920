package io.example.orderedcommit;

import java.util.Map;

/**
 * Standalone entry point.
 *
 * <p>All flags are optional:
 * <pre>
 *   java ... Main --port 8080 --host 0.0.0.0 \
 *                 --inflight-cap 8 --buffer-cap 16 \
 *                 --default-delay-ms 500 --timeout-ms 2000 \
 *                 --max-attempts 3 --retry-delay-ms 100 --http-threads 16
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        Map<String, String> opts = Args.parse(args);

        int port = Args.integer(opts, "port", 8080);
        String host = opts.getOrDefault("host", "0.0.0.0");
        int inflightCap = Args.integer(opts, "inflight-cap", 8);
        long bufferCap = Args.longInteger(opts, "buffer-cap", 16);
        long defaultDelay = Args.longInteger(opts, "default-delay-ms", 500);
        long timeout = Args.longInteger(opts, "timeout-ms", 2000);
        int maxAttempts = Args.integer(opts, "max-attempts", 3);
        long retryDelay = Args.longInteger(opts, "retry-delay-ms", 100);
        int httpThreads = Args.integer(opts, "http-threads", 16);

        OrderedEventService.Config config =
                new OrderedEventService.Config(
                        inflightCap, bufferCap, defaultDelay, timeout, maxAttempts, retryDelay);
        OrderedEventService service =
                new OrderedEventService(config, new SleepingEventProcessor());
        HttpEventServer server = new HttpEventServer(service, httpThreads);

        Runtime.getRuntime()
                .addShutdownHook(
                        new Thread(
                                () -> {
                                    System.out.println("shutting down...");
                                    try {
                                        server.stop();
                                    } catch (Exception ignored) {
                                        // exit anyway
                                    }
                                },
                                "shutdown"));

        server.start(port, host);
        System.out.println("ordered-event-service listening on http://" + host + ":" + server.port());
        System.out.println("  inflightCap=" + inflightCap + " bufferCap=" + bufferCap
                + " defaultDelayMs=" + defaultDelay + " timeoutMs=" + timeout
                + " maxAttempts=" + maxAttempts + " retryDelayMs=" + retryDelay);
    }
}
