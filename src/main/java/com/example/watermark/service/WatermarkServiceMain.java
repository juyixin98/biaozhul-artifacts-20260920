package com.example.watermark.service;

/**
 * Entry point. Usage: {@code java com.example.watermark.service.WatermarkServiceMain [port]}
 * (default port 8080; {@code 0} picks an ephemeral port).
 */
public final class WatermarkServiceMain {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            port = Integer.parseInt(args[0]);
        }
        WatermarkHttpServer server = new WatermarkHttpServer(port);
        server.start();
        System.out.println("Watermark service listening on port " + server.getPort());
        System.out.println("Endpoints: GET /health | POST /api/run | POST /api/sessions");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down...");
            server.close();
        }));
        // Block forever; shut down with Ctrl-C (or SIGTERM).
        Thread.currentThread().join();
    }

    private WatermarkServiceMain() {
    }
}
