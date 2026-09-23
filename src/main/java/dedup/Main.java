package dedup;

import dedup.http.DedupHttpServer;

import java.util.Map;

/**
 * Entry point.
 *
 * Usage:
 *   java -cp build/classes dedup.Main [port]
 *
 * Port defaults to 8080, or use the DEDUP_PORT environment variable.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port;
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        } else {
            String env = System.getenv("DEDUP_PORT");
            port = env != null ? Integer.parseInt(env) : 8080;
        }

        DedupService service = new DedupService();
        DedupHttpServer http = new DedupHttpServer(port, service);
        http.start();

        Map<String, Object> banner = new java.util.LinkedHashMap<>();
        banner.put("service", "event-id-dedup");
        banner.put("port", http.port());
        banner.put("health", "GET http://localhost:" + http.port() + "/health");
        System.out.println(Json.writePretty(banner));

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down...");
            http.stop();
        }));

        // Block forever; the HTTP executor threads are daemons.
        Thread.currentThread().join();
    }
}
