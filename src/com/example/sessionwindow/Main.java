package com.example.sessionwindow;

import com.example.sessionwindow.json.Json;
import com.example.sessionwindow.model.ResultRecord;
import com.example.sessionwindow.service.BatchProcessor;
import com.example.sessionwindow.service.HttpServerRunner;
import com.example.sessionwindow.service.Requests;
import com.example.sessionwindow.service.SessionWindowService;
import com.example.sessionwindow.time.ScheduledTimerService;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * Entry point.
 *
 * <pre>
 *   java -cp classes com.example.sessionwindow.Main serve [--port 8080]
 *   java -cp classes com.example.sessionwindow.Main run --file samples/bridge.json [--no-flush]
 *   java -cp classes com.example.sessionwindow.Main run --stdin &lt; request.json
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "serve" -> serve(args);
            case "run" -> run(args);
            case "-h", "--help", "help" -> usage();
            default -> {
                System.err.println("unknown command: " + args[0]);
                usage();
                System.exit(2);
            }
        }
    }

    private static void serve(String[] args) throws IOException {
        int port = 8080;
        for (int i = 1; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            } else {
                System.err.println("unknown option: " + args[i]);
                System.exit(2);
            }
        }
        SessionWindowService service = new SessionWindowService(new ScheduledTimerService());
        HttpServerRunner runner = new HttpServerRunner(port, service);
        runner.start();
        System.out.println("event-time session window service listening on http://127.0.0.1:" + runner.port());
        System.out.println("endpoints:");
        System.out.println("  GET  /health");
        System.out.println("  POST /session-windows/run");
        System.out.println("  GET  /session-windows/pipelines");
        System.out.println("  POST/GET/DELETE /session-windows/pipelines/{id}");
        System.out.println("  POST /session-windows/pipelines/{id}/events");

        Runtime.getRuntime().addShutdownHook(new Thread(runner::stop));
        // Block until killed; the daemon executor keeps the JVM alive only via this latch.
        try {
            Thread.currentThread().join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void run(String[] args) throws IOException {
        String file = null;
        boolean stdin = false;
        boolean flushAtEnd = true;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--file" -> file = args[++i];
                case "--stdin" -> stdin = true;
                case "--no-flush" -> flushAtEnd = false;
                default -> {
                    System.err.println("unknown option: " + args[i]);
                    System.exit(2);
                }
            }
        }
        String text;
        if (file != null) {
            text = Files.readString(Path.of(file), StandardCharsets.UTF_8);
        } else if (stdin) {
            text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        } else {
            System.err.println("run requires --file <path> or --stdin");
            System.exit(2);
            return;
        }

        Map<String, Object> req = Json.parseObject(text);
        long gap = Json.lng(req, "gap", 10L);
        long allowedLateness = Json.lng(req, "allowedLateness", 0L);
        List<BatchProcessor.Item> items = Requests.parseItems(req);

        BatchProcessor.Outcome outcome = BatchProcessor.run(gap, allowedLateness, items, flushAtEnd);
        System.out.println("=== records (explicit update stream) ===");
        for (ResultRecord r : outcome.records) {
            System.out.println(r);
        }
        System.out.println();
        System.out.println("=== materialized results ===");
        System.out.println(Json.pretty(outcome.resultsAsLists().entrySet().stream()
                .collect(java.util.stream.Collectors.toMap(
                        Map.Entry::getKey,
                        e -> e.getValue().stream().map(a -> a.toJson()).toList(),
                        (a, b) -> a,
                        java.util.LinkedHashMap::new))));
        System.out.println("matches offline reference: " + outcome.matchesReference);
        if (flushAtEnd) {
            System.out.println("state after flush: retainedKeys=" + outcome.remainingKeys
                    + " retainedSessions=" + outcome.remainingSessions
                    + " allCleaned=" + (outcome.remainingKeys == 0 && outcome.remainingSessions == 0));
        }
        if (!outcome.matchesReference) {
            System.exit(1);
        }
    }

    private static void usage() {
        System.out.println("""
                Usage:
                  serve [--port 8080]                       start the JSON HTTP service
                  run --file <request.json> [--no-flush]    run one batch request
                  run --stdin [--no-flush]                  read request JSON from stdin

                Request format (see samples/*.json):
                  {"gap":5,"allowedLateness":5,"flushAtEnd":true,
                   "items":[{"key":"u1","timestamp":1},
                            {"watermark":6},
                            {"key":"u1","timestamp":4}]}
                """);
    }
}
