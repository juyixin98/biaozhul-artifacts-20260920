package orderedevents;

import orderedevents.service.EventService;
import orderedevents.service.ServerConfig;
import orderedevents.web.ApiServer;

/**
 * Entry point. Pure JDK — no third-party dependencies.
 *
 * <pre>
 * java -cp classes orderedevents.Main [options]
 *   --port PORT                 (default 8080; 0 = ephemeral)
 *   --max-in-flight N           per-partition buffer cap (default 8)
 *   --max-concurrent N          per-partition running-attempt cap (default 2)
 *   --default-max-attempts N    default retry budget incl. first try (default 3)
 *   --default-timeout-ms N      default per-attempt timeout (default 1000)
 *   --retry-backoff-ms N        fixed backoff between attempts (default 50)
 * </pre>
 */
public final class Main {

    public static void main(String[] args) {
        ServerConfig config = parseArgs(args);
        EventService service = new EventService(config);
        ApiServer server = new ApiServer(config, service);
        server.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down...");
            try {
                server.stop();
            } finally {
                service.shutdown();
            }
        }, "shutdown"));

        System.out.println("ordered-events service listening on http://localhost:" + server.boundPort());
        System.out.println("  per-partition maxInFlight=" + config.maxInFlight()
                + ", maxConcurrent=" + config.maxConcurrent()
                + ", defaultMaxAttempts=" + config.defaultMaxAttempts()
                + ", defaultTimeoutMillis=" + config.defaultTimeoutMillis());
    }

    private static ServerConfig parseArgs(String[] args) {
        int port = 8080;
        int maxInFlight = 8;
        int maxConcurrent = 2;
        int defaultMaxAttempts = 3;
        long defaultTimeout = 1000;
        long backoff = 50;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = intArg(args, ++i, "--port");
                case "--max-in-flight" -> maxInFlight = intArg(args, ++i, "--max-in-flight");
                case "--max-concurrent" -> maxConcurrent = intArg(args, ++i, "--max-concurrent");
                case "--default-max-attempts" -> defaultMaxAttempts = intArg(args, ++i, "--default-max-attempts");
                case "--default-timeout-ms" -> defaultTimeout = longArg(args, ++i, "--default-timeout-ms");
                case "--retry-backoff-ms" -> backoff = longArg(args, ++i, "--retry-backoff-ms");
                case "-h", "--help" -> {
                    System.out.println("Usage: orderedevents.Main [--port N] [--max-in-flight N] "
                            + "[--max-concurrent N] [--default-max-attempts N] "
                            + "[--default-timeout-ms N] [--retry-backoff-ms N]");
                    System.exit(0);
                }
                default -> {
                    System.err.println("unknown argument: " + args[i]);
                    System.exit(2);
                }
            }
        }
        if (maxInFlight <= 0 || maxConcurrent <= 0 || defaultMaxAttempts <= 0
                || defaultTimeout <= 0 || backoff < 0) {
            System.err.println("limits must be positive (backoff may be zero)");
            System.exit(2);
        }
        return new ServerConfig(port, maxInFlight, maxConcurrent, defaultMaxAttempts,
                10, defaultTimeout, 60_000, backoff, 30_000);
    }

    private static int intArg(String[] args, int i, String name) {
        try {
            return Integer.parseInt(args[i]);
        } catch (Exception e) {
            System.err.println(name + " requires an integer");
            System.exit(2);
            return 0;
        }
    }

    private static long longArg(String[] args, int i, String name) {
        try {
            return Long.parseLong(args[i]);
        } catch (Exception e) {
            System.err.println(name + " requires a long");
            System.exit(2);
            return 0;
        }
    }

    private Main() {
    }
}
