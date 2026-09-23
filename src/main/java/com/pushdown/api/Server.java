package com.pushdown.api;

import com.pushdown.exec.Engine;
import com.pushdown.json.Json;
import com.pushdown.opt.Optimizer;
import com.pushdown.plan.Plan;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON-over-HTTP entry point (JDK built-in HttpServer, no web framework).
 *
 * POST /query
 *   {
 *     "tables": { "T": {"columns": [...], "rows": [[...], ...]}, ... },
 *     "plan":   { "type": "filter" | "project" | "join" | "scan", ... },
 *     "optimize": true,                // optional, default true
 *     "exportDir": "out/export1"       // optional: also write plan.json + data.json
 *   }
 * Response: originalPlan, optimizedPlan, reasons, schema, rows.
 */
public final class Server {

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        HttpServer server = create(port);
        server.start();
        System.out.println("pushdown engine listening on port " + port);
    }

    public static HttpServer create(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/query", Server::handleQuery);
        server.createContext("/health", ex -> respond(ex, 200, "{\"status\":\"ok\"}"));
        return server;
    }

    @SuppressWarnings("unchecked")
    static void handleQuery(HttpExchange ex) throws IOException {
        if (!ex.getRequestMethod().equalsIgnoreCase("POST")) {
            respond(ex, 405, "{\"error\":\"POST only\"}");
            return;
        }
        try {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            Map<String, Object> req = Json.parseObject(body);
            Map<String, Engine.Table> catalog =
                    JsonPlan.tablesFromJson((Map<String, Object>) req.get("tables"));
            Plan plan = JsonPlan.planFromJson((Map<String, Object>) req.get("plan"));
            boolean optimize = !Boolean.FALSE.equals(req.get("optimize"));

            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("originalPlan", JsonPlan.planToJson(plan));
            Plan effective = plan;
            if (optimize) {
                Optimizer.Result r = Optimizer.optimize(plan, catalog);
                effective = r.plan();
                resp.put("optimizedPlan", JsonPlan.planToJson(effective));
                List<Object> rs = new ArrayList<>();
                for (Optimizer.Reason reason : r.reasons()) {
                    Map<String, Object> rm = new LinkedHashMap<>();
                    rm.put("rule", reason.rule());
                    rm.put("detail", reason.detail());
                    rs.add(rm);
                }
                resp.put("reasons", rs);
            }
            Engine.Output out = Engine.execute(effective, catalog);
            resp.put("schema", out.schema());
            resp.put("rows", out.rows());

            if (req.get("exportDir") instanceof String dir) {
                Path d = Path.of(dir);
                Files.createDirectories(d);
                Files.writeString(d.resolve("plan.json"),
                        Json.write(resp.get(optimize ? "optimizedPlan" : "originalPlan")));
                Map<String, Object> data = new LinkedHashMap<>();
                data.put("schema", out.schema());
                data.put("rows", out.rows());
                Files.writeString(d.resolve("data.json"), Json.write(data));
                resp.put("exportedTo", d.toAbsolutePath().toString());
            }
            respond(ex, 200, Json.write(resp));
        } catch (Exception e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", String.valueOf(e.getMessage()));
            respond(ex, 400, Json.write(err));
        }
    }

    private static void respond(HttpExchange ex, int code, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        ex.getResponseBody().write(bytes);
        ex.close();
    }
}
