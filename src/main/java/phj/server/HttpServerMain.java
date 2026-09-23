package phj.server;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import phj.QueryRunner;
import phj.json.Json;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 内置 HTTP JSON 入口（com.sun.net.httpserver，JDK 自带）。
 *
 * 路由：
 *   GET  /health    -> {"ok":true}
 *   POST /query     -> 请求体即查询请求 JSON，响应体即查询响应 JSON
 *
 * HTTP 状态码：200 成功；400 请求非法；507 磁盘额度耗尽；500 内部错误。
 *
 * 用法：java phj.server.HttpServerMain [port]   （默认 8080，port=0 随机端口）
 */
public final class HttpServerMain {

    private HttpServerMain() {}

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", HttpServerMain::health);
        server.createContext("/query", HttpServerMain::query);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        System.out.println("PHJ HTTP 服务已启动：http://localhost:" + server.getAddress().getPort()
                + "  （POST /query，GET /health）");
    }

    static void health(HttpExchange ex) throws IOException {
        writeJson(ex, 200, "{\"ok\":true,\"service\":\"partitioned-hash-join\"}");
    }

    static void query(HttpExchange ex) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
                writeJson(ex, 405, "{\"ok\":false,\"error\":\"仅支持 POST\"}");
                return;
            }
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            Map<String, Object> respMap;
            int httpStatus;
            try {
                Map<String, Object> req = Json.asObj(Json.parse(body), "请求");
                respMap = QueryRunner.run(req);
                httpStatus = 200;
            } catch (Throwable t) {
                respMap = QueryRunner.errorResponse(t);
                Object code = respMap.get("errorCode");
                httpStatus = code instanceof Number n ? n.intValue() : 500;
            }
            writeJson(ex, httpStatus, Json.write(respMap));
        } catch (Throwable t) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("ok", false);
            err.put("error", t.getMessage());
            writeJson(ex, 500, Json.write(err));
        }
    }

    private static void writeJson(HttpExchange ex, int status, String json) throws IOException {
        byte[] payload = json.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }
}
