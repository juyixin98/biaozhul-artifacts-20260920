package topk;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * JSON-over-HTTP entry point (JDK built-in server, no external deps).
 *
 *   POST /load         {"rows":[{"group":"a","value":5}, ...]}   (replaces dataset)
 *   POST /append       {"rows":[...]}                            (appends)
 *   POST /query        {"k":3,"shards":4,"mergeOrder":[2,0,1,3]} (mergeOrder optional)
 *   GET  /export/data  dataset with assigned sequence numbers
 *   GET  /export/plan  execution plan of the most recent query
 */
public final class Server {

    private final DataSet data = new DataSet();
    private final QueryEngine engine;
    private volatile ExecutionPlan lastPlan;

    public Server(int budget) {
        this.engine = new QueryEngine(data, budget);
    }

    public static void main(String[] args) throws IOException {
        int port = 8080;
        int budget = 1000;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port": port = Integer.parseInt(args[++i]); break;
                case "--budget": budget = Integer.parseInt(args[++i]); break;
                default:
                    System.err.println("unknown arg: " + args[i]);
                    System.err.println("usage: Server [--port N] [--budget N]");
                    System.exit(2);
            }
        }
        Server srv = new Server(budget);
        HttpServer http = HttpServer.create(new InetSocketAddress(port), 0);
        http.createContext("/load", srv::handleLoad);
        http.createContext("/append", srv::handleAppend);
        http.createContext("/query", srv::handleQuery);
        http.createContext("/export/data", srv::handleExportData);
        http.createContext("/export/plan", srv::handleExportPlan);
        http.setExecutor(null);
        http.start();
        System.out.println("listening on http://127.0.0.1:" + port + " (budget=" + budget + ")");
    }

    private void handleLoad(HttpExchange ex) throws IOException {
        handleMutation(ex, true);
    }

    private void handleAppend(HttpExchange ex) throws IOException {
        handleMutation(ex, false);
    }

    private void handleMutation(HttpExchange ex, boolean replace) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            Object rowsObj = body.get("rows");
            if (!(rowsObj instanceof List)) {
                throw new IllegalArgumentException("missing required array field 'rows'");
            }
            if (replace) {
                synchronized (data) {
                    data.reset();
                    int n = loadRows(data, (List<?>) rowsObj);
                    respond(ex, 200, "{\"loaded\":" + n + ",\"replaced\":true}");
                }
            } else {
                int n = loadRows(data, (List<?>) rowsObj);
                respond(ex, 200, "{\"loaded\":" + n + ",\"replaced\":false}");
            }
        } catch (IllegalArgumentException | Json.JsonException e) {
            respond(ex, 400, errorJson(e.getMessage()));
        }
    }

    private int loadRows(DataSet ds, List<?> rows) {
        int n = 0;
        for (Object o : rows) {
            if (!(o instanceof Map)) {
                throw new IllegalArgumentException("each row must be an object {group, value}");
            }
            Map<?, ?> m = (Map<?, ?>) o;
            Object g = m.get("group");
            Object v = m.get("value");
            if (!(g instanceof String)) {
                throw new IllegalArgumentException("row missing string field 'group'");
            }
            if (!(v instanceof Number)) {
                throw new IllegalArgumentException("row missing numeric field 'value'");
            }
            ds.add((String) g, ((Number) v).longValue());
            n++;
        }
        return n;
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            TopKQuery q = TopKQuery.fromJson(body, engine.budget());
            QueryEngine.Result result = engine.execute(q);
            lastPlan = result.plan;
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("k", q.k());
            out.put("result", result.toJson());
            respond(ex, 200, Json.write(out));
        } catch (IllegalArgumentException | Json.JsonException e) {
            respond(ex, 400, errorJson(e.getMessage()));
        }
    }

    private void handleExportData(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        respond(ex, 200, Json.write(data.toJson()));
    }

    private void handleExportPlan(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        ExecutionPlan p = lastPlan;
        if (p == null) {
            respond(ex, 404, errorJson("no query executed yet"));
            return;
        }
        respond(ex, 200, p.toJsonString());
    }

    private static boolean requireMethod(HttpExchange ex, String method) throws IOException {
        if (!ex.getRequestMethod().equalsIgnoreCase(method)) {
            respond(ex, 405, errorJson("method " + ex.getRequestMethod() + " not allowed, use " + method));
            return false;
        }
        return true;
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static String errorJson(String msg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", msg == null ? "bad request" : msg);
        return Json.write(m);
    }

    private static void respond(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
