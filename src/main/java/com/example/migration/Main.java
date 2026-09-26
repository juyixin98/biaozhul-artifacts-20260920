package com.example.migration;

import com.example.migration.data.DemoRequests;
import com.example.migration.data.FixedData;
import com.example.migration.json.Json;
import com.example.migration.model.PlanRequest;
import com.example.migration.model.PlanResponse;
import com.example.migration.service.PlanningService;
import com.example.migration.service.TzdbInfo;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * Pure backend entry point.
 *
 * <pre>
 *   java -jar app.jar plan request.json     plan from a JSON file ("-" = stdin)
 *   java -jar app.jar demo [scenario]       run a built-in fixed-data scenario
 *   java -jar app.jar scenarios             list built-in scenarios
 *   java -jar app.jar env                   print tzdb/java environment info
 *   java -jar app.jar serve [port]          JSON HTTP API (default 8080)
 * </pre>
 *
 * HTTP: {@code POST /plan} with a PlanRequest JSON body; {@code GET /health}.
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        String command = args.length == 0 ? "demo" : args[0];
        int exit = switch (command) {
            case "plan" -> plan(args.length > 1 ? args[1] : "-");
            case "demo" -> demo(args.length > 1 ? args[1] : FixedData.DEFAULT_SCENARIO);
            case "scenarios" -> scenarios();
            case "env" -> env();
            case "serve" -> serve(args.length > 1 ? Integer.parseInt(args[1]) : 8080);
            default -> usage();
        };
        System.exit(exit);
    }

    private static int plan(String location) throws IOException {
        String body = "-".equals(location)
                ? new String(System.in.readAllBytes(), StandardCharsets.UTF_8)
                : Files.readString(Path.of(location));
        return handleRequestJson(body, System.out);
    }

    private static int demo(String scenario) throws IOException {
        PlanRequest request = DemoRequests.forScenario(scenario);
        String json = Json.mapper().writerWithDefaultPrettyPrinter().writeValueAsString(request);
        return handleRequestJson(json, System.out);
    }

    /** Parse + plan + print. Package-visible for reuse by the HTTP layer. */
    static int handleRequestJson(String requestJson, OutputStream out) throws IOException {
        try {
            PlanRequest request = Json.mapper().readValue(requestJson, PlanRequest.class);
            PlanResponse response = new PlanningService("request-provided").plan(request);
            out.write(Json.mapper().writerWithDefaultPrettyPrinter()
                    .writeValueAsBytes(response));
            out.write('\n');
            return 0;
        } catch (Exception e) {
            PlanResponse err = new PlanResponse("ERROR", false,
                    new PlanResponse.EnvironmentInfo(TzdbInfo.version(),
                            System.getProperty("java.version"), "request-provided"),
                    null, null, null, null, List.of(), List.of(), List.of(),
                    new PlanResponse.ApiError("BAD_REQUEST",
                            e.getClass().getSimpleName() + ": " + e.getMessage()));
            out.write(Json.mapper().writerWithDefaultPrettyPrinter().writeValueAsBytes(err));
            out.write('\n');
            return 2;
        }
    }

    private static int scenarios() {
        System.out.println(String.join("\n", FixedData.scenarios()));
        return 0;
    }

    private static int env() {
        System.out.println("tzdb.version=" + TzdbInfo.version());
        System.out.println("java.version=" + System.getProperty("java.version"));
        System.out.println("java.vm.name=" + System.getProperty("java.vm.name"));
        return 0;
    }

    private static int usage() {
        System.err.println("usage: plan <file|-> | demo [scenario] | scenarios | env | serve [port]");
        return 64;
    }

    // ------------------------------------------------------------------ HTTP API

    private static int serve(int port) throws IOException, InterruptedException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", Main::health);
        server.createContext("/plan", Main::planEndpoint);
        server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(4));
        server.start();
        System.out.println("migration-planner listening on http://localhost:" + port
                + " (POST /plan, GET /health)");
        Thread.currentThread().join();
        return 0;
    }

    private static void health(HttpExchange exchange) throws IOException {
        String body = "{\"status\":\"UP\",\"tzdbVersion\":\"" + TzdbInfo.version() + "\"}";
        write(exchange, 200, body.getBytes(StandardCharsets.UTF_8));
    }

    private static void planEndpoint(HttpExchange exchange) throws IOException {
        if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
            write(exchange, 405, "{\"error\":\"method not allowed\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }
        byte[] requestBytes = exchange.getRequestBody().readAllBytes();
        java.io.ByteArrayOutputStream result = new java.io.ByteArrayOutputStream();
        int code = handleRequestJson(
                new String(requestBytes, StandardCharsets.UTF_8), result);
        write(exchange, code == 0 ? 200 : 400, result.toByteArray());
    }

    private static void write(HttpExchange exchange, int status, byte[] body) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(body);
        }
    }
}
