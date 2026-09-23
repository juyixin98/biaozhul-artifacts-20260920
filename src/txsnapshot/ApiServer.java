package txsnapshot;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@code com.sun.net.httpserver.HttpServer} 的纯后端 HTTP 接口层。
 * 无任何第三方依赖。
 */
public final class ApiServer {

    private final HttpServer server;
    private final StreamEngine engine;
    private final boolean debug;

    private ApiServer(HttpServer server, StreamEngine engine, boolean debug) {
        this.server = server;
        this.engine = engine;
        this.debug = debug;
    }

    public static ApiServer start(Path dataDir, int port, boolean debug) throws IOException {
        Fault fault = new Fault();
        StreamEngine engine = StreamEngine.open(dataDir, fault);
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        ApiServer api = new ApiServer(server, engine, debug);
        server.createContext("/health", api::health);
        server.createContext("/ingest", api::ingest);
        server.createContext("/state", api::state);
        server.createContext("/outputs", api::outputs);
        if (debug) {
            server.createContext("/debug/fault", api::armFault);
        }
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        return api;
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
        try {
            engine.close();
        } catch (IOException e) {
            // 关闭阶段尽力而为
            e.printStackTrace();
        }
    }

    private void health(HttpExchange ex) throws IOException {
        json(ex, 200, Map.of("status", engine.status().get("crashed").equals(Boolean.TRUE)
                ? "crashed" : "ok"));
    }

    private void ingest(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            json(ex, 405, Map.of("error", "POST only"));
            return;
        }
        String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        long value;
        try {
            Map<String, Object> req = Json.object(body);
            value = Json.lng(req, "value");
        } catch (RuntimeException e) {
            json(ex, 400, Map.of("error", "invalid JSON, expected {\"value\": <integer>}",
                    "detail", String.valueOf(e.getMessage())));
            return;
        }
        try {
            long offset = engine.ingest(value);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("offset", offset);
            resp.put("committedOutputs", engine.status().get("committedOutputs"));
            json(ex, 200, resp);
        } catch (IllegalStateException e) {
            json(ex, 503, Map.of("error", e.getMessage()));
        } catch (Exception e) {
            json(ex, 500, Map.of("error", String.valueOf(e.getMessage())));
        }
    }

    private void state(HttpExchange ex) throws IOException {
        json(ex, 200, engine.status());
    }

    private void outputs(HttpExchange ex) throws IOException {
        json(ex, 200, Map.of("outputs", engine.outputs()));
    }

    /**
     * 故障注入（仅 --debug 启动时可用）。
     * JSON：{"point":"AFTER_STATE_PERSISTED","mode":"HALT","offset":3}
     * point: DURING_PROCESSING | DURING_PREPARE | AFTER_STATE_PERSISTED | DURING_COMMIT
     * mode:  HALT（杀进程）| EXCEPTION（进程内崩溃态）
     * offset 可省略，表示下一条任意偏移触发。
     */
    private void armFault(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            json(ex, 405, Map.of("error", "POST only"));
            return;
        }
        try {
            Map<String, Object> req = Json.object(
                    new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            Fault.Point point = Fault.Point.valueOf(Json.str(req, "point"));
            String modeStr = req.containsKey("mode") ? Json.str(req, "mode") : "HALT";
            Fault.Mode mode = Fault.Mode.valueOf(modeStr);
            long offset = req.containsKey("offset") ? Json.lng(req, "offset") : -1L;
            engine.fault().arm(point, mode, offset);
            json(ex, 200, Map.of("armed", point.name(), "mode", mode.name(), "offset", offset));
        } catch (Exception e) {
            json(ex, 400, Map.of("error", String.valueOf(e.getMessage())));
        }
    }

    private static void json(HttpExchange ex, int code, Object payload) throws IOException {
        byte[] data = (Json.dump(payload) + "\n").getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, data.length);
        ex.getResponseBody().write(data);
        ex.close();
    }
}
