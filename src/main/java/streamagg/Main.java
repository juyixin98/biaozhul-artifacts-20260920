package streamagg;

import streamagg.server.AggregateHttpServer;

/**
 * Entry point: starts the JSON service on 127.0.0.1.
 *
 * <p>Port from first argument or env PORT (default 8080).
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        } else if (System.getenv("PORT") != null) {
            port = Integer.parseInt(System.getenv("PORT"));
        }

        AggregateHttpServer http = new AggregateHttpServer(port);
        http.start();

        System.out.println("streamagg service listening on http://127.0.0.1:" + http.getPort());
        System.out.println("endpoints:");
        System.out.println("  POST /v1/events/ingest   - one lifecycle operation");
        System.out.println("  POST /v1/events/batch    - ordered operation batch");
        System.out.println("  GET  /v1/aggregates      - per-key sum/count (add ?verify=1)");
        System.out.println("  GET  /v1/events          - live events + buffered ops");
        System.out.println("  GET  /v1/ledger          - resolved event ledger");
        System.out.println("  POST /v1/replay          - reference replay check");
        System.out.println("  POST /v1/emit            - emit one output snapshot");
        System.out.println("  POST /v1/admin/reset     - clear state");
        System.out.println("  GET  /health             - liveness");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down...");
            http.close();
        }));

        // Block forever; the HTTP threads are daemons.
        Thread.currentThread().join();
    }
}
