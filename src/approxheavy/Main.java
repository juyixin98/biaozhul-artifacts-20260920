package approxheavy;

import approxheavy.core.Clock;
import approxheavy.core.ExecutorScheduler;
import approxheavy.core.SystemClock;
import approxheavy.server.ApiServer;
import approxheavy.server.StreamRegistry;

/**
 * HTTP service entry point.
 *
 * <p>Usage: {@code java approxheavy.Main [port]} (default 8080). A default
 * stream "events" is created at startup with width=32, depth=5, seed=0,
 * 60-second tumbling windows.
 */
public final class Main {
    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;

        Clock clock = new SystemClock();
        ExecutorScheduler scheduler = new ExecutorScheduler();
        StreamRegistry registry = new StreamRegistry(clock, scheduler);

        StreamRegistry.Config config = new StreamRegistry.Config();
        registry.create("events", config);

        ApiServer server = new ApiServer(port, registry, clock);
        server.start();
        System.out.println("approx-heavy service listening on http://localhost:" + server.port());
        System.out.println("default stream 'events' ready: width=" + config.width
                + " depth=" + config.depth + " seed=" + config.seed
                + " windowMillis=" + config.windowMillis);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.close();
            scheduler.close();
        }));
        // Block forever; shutdown hook closes resources.
        Thread.currentThread().join();
    }
}
