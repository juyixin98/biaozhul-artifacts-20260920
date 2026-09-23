package joinplanner;

import joinplanner.web.Server;

/**
 * Entry point. Usage: {@code java joinplanner.Main [port]} (default 8080).
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        }
        Server server = new Server(port);
        server.start();
        int bound = server.boundPort();
        System.out.println("join-order-planner listening on http://localhost:" + bound);
        System.out.println("  POST /api/plan");
        System.out.println("  POST /api/simulate");
        System.out.println("  GET  /health");

        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));

        // Keep the main thread alive (HTTP server uses daemon threads).
        Thread.currentThread().join();
    }
}
