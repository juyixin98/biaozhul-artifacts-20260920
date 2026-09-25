package dev.timeprecision;

import dev.timeprecision.http.TimePrecisionServer;

/** Entry point: starts the JSON-over-HTTP conversion service. */
public final class Main {

    private static final int DEFAULT_PORT = 8080;

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = resolvePort(args);
        TimePrecisionServer server = TimePrecisionServer.start(port);
        System.out.println("time-precision-converter listening on http://127.0.0.1:" + server.port());
        System.out.println("tzdb version: " + Meta.tzdbVersion());
        Thread.currentThread().join();
    }

    private static int resolvePort(String[] args) {
        if (args.length > 0) {
            return Integer.parseInt(args[0]);
        }
        String env = System.getenv("PORT");
        return env != null ? Integer.parseInt(env) : DEFAULT_PORT;
    }
}
