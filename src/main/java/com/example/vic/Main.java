package com.example.vic;

import com.example.vic.http.ApiServer;

/**
 * Starts the JSON backend. Usage: java -jar version-interval-coverage.jar [port]
 */
public final class Main {

    private static final int DEFAULT_PORT = 8080;

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : DEFAULT_PORT;
        ApiServer server = ApiServer.create(port);
        server.start();
        System.out.println("version-interval-coverage listening on port " + server.port());
        System.out.println("endpoints: " + server.describe().get("endpoints"));
    }
}
