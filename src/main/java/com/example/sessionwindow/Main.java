package com.example.sessionwindow;

import java.nio.file.Path;

/**
 * Entry point.
 *
 *   java -cp out/classes com.example.sessionwindow.Main \
 *        [--port 8080] [--gap 10] [--allowed-lateness 10] [--data-dir run/data]
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = intArg(args, "--port", 8080);
        long gap = longArg(args, "--gap", 10);
        long allowedLateness = longArg(args, "--allowed-lateness", 10);
        String dataDir = strArg(args, "--data-dir", "run/data");

        EventLog log = new EventLog(Path.of(dataDir, "event-log.jsonl"));
        SessionEngine engine = new SessionEngine(gap, allowedLateness, log);
        ApiServer api = new ApiServer(port, engine);
        api.start();

        System.out.println("Dynamic session window service started");
        System.out.println("  port             : " + api.port());
        System.out.println("  gap              : " + gap);
        System.out.println("  allowed-lateness : " + allowedLateness);
        System.out.println("  event log        : " + log.path().toAbsolutePath());
        System.out.println("  watermark        : " + engine.watermark());
        System.out.println();
        System.out.println("Endpoints:");
        System.out.println("  POST /events        {\"key\":\"userA\",\"ts\":20,\"clientId\":\"...\"}");
        System.out.println("  POST /events/batch  {\"events\":[...]}");
        System.out.println("  POST /watermark     {\"watermark\":20}");
        System.out.println("  GET  /sessions[?key=userA]");
        System.out.println("  GET  /output[?afterSeq=N]");
        System.out.println("  GET  /watermark  GET /stats  POST /reset  GET /health");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            api.stop();
            log.close();
        }));
    }

    private static String strArg(String[] args, String name, String dflt) {
        for (int i = 0; i < args.length - 1; i++) {
            if (args[i].equals(name)) {
                return args[i + 1];
            }
        }
        return dflt;
    }

    private static int intArg(String[] args, String name, int dflt) {
        String v = strArg(args, name, null);
        if (v == null) {
            return dflt;
        }
        try {
            return Integer.parseInt(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("Invalid integer for " + name + ": " + v);
        }
    }

    private static long longArg(String[] args, String name, long dflt) {
        String v = strArg(args, name, null);
        if (v == null) {
            return dflt;
        }
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("Invalid integer for " + name + ": " + v);
        }
    }
}
