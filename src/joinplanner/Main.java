package joinplanner;

import joinplanner.http.ApiServer;

/**
 * Entry point. Usage: {@code java -cp out joinplanner.Main [port]} (default 8080).
 * No third-party dependencies; JDK 17+ only.
 */
public final class Main {

    private Main() {}

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            try {
                port = Integer.parseInt(args[0]);
            } catch (NumberFormatException e) {
                System.err.println("Usage: joinplanner.Main [port]");
                System.exit(2);
            }
        }
        ApiServer server = new ApiServer(port);
        server.start();
        System.out.println("Join order planner listening on http://localhost:" + server.actualPort());
        System.out.println("Endpoints: GET /health | POST /plan | POST /enumerate | POST /simulate");

        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
        Thread.currentThread().join();
    }
}
