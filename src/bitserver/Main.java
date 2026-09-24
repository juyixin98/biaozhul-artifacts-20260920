package bitserver;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * 基于 JDK 内置 {@code com.sun.net.httpserver.HttpServer} 的 HTTP 服务入口。
 * 零第三方依赖。
 *
 * 路由：
 *   GET  /              接口说明
 *   GET  /health        健康检查与加载状态
 *   POST /load          加载/替换数据集（CSV 文本、csvPath 或 columns+rows）
 *   POST /query         布尔表达式查询，返回命中行 ID
 *   POST /delete        按 ids 或 where 软删除
 *   POST /restore       按 ids 或 where 恢复
 *   GET  /stats         索引空间统计（未压缩 / RLE 压缩）
 *
 * 用法：java bitserver.Main [port] [--autoload /path/to/data.csv]
 * 默认端口 8080。
 */
public final class Main {

    private static final int MAX_BODY = 256 * 1024 * 1024; // 256 MiB

    private final IndexService service;

    public Main(IndexService service) {
        this.service = service;
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String autoload = null;
        for (int i = 0; i < args.length; i++) {
            if ("--autoload".equals(args[i])) {
                autoload = requireArgValue(args, ++i, "--autoload");
            } else if (args[i].startsWith("--port=")) {
                port = parsePort(args[i].substring("--port=".length()));
            } else if ("--port".equals(args[i])) {
                port = parsePort(requireArgValue(args, ++i, "--port"));
            } else {
                port = parsePort(args[i]);
            }
        }

        IndexService service = new IndexService();
        if (autoload != null) {
            Path p = Path.of(autoload);
            if (!Files.isRegularFile(p)) {
                System.err.println("自动加载文件不存在: " + p.toAbsolutePath());
                System.exit(2);
            }
            service.loadCsvFile(p);
            System.out.println("已自动加载数据集: " + p.toAbsolutePath());
        }

        HttpServer server = startServer(service, port);
        System.out.println("位图索引服务已启动: http://localhost:" + port + "/");
        System.out.println("接口说明: GET /     健康检查: GET /health");
    }

    static HttpServer startServer(IndexService service, int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        Main handler = new Main(service);
        server.createContext("/", handler::route);
        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "bitserver-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.start();
        return server;
    }

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/":
                    if ("GET".equals(method)) {
                        sendJson(ex, 200, apiHelp());
                    } else {
                        sendError(ex, 405, "只支持 GET");
                    }
                    break;
                case "/health":
                    if ("GET".equals(method)) {
                        handleHealth(ex);
                    } else {
                        sendError(ex, 405, "只支持 GET");
                    }
                    break;
                case "/load":
                    if ("POST".equals(method)) {
                        handleLoad(ex);
                    } else {
                        sendError(ex, 405, "只支持 POST");
                    }
                    break;
                case "/query":
                    if ("POST".equals(method)) {
                        handleQuery(ex);
                    } else {
                        sendError(ex, 405, "只支持 POST");
                    }
                    break;
                case "/delete":
                case "/restore":
                    if ("POST".equals(method)) {
                        handleDeleteOrRestore(ex, "/delete".equals(path));
                    } else {
                        sendError(ex, 405, "只支持 POST");
                    }
                    break;
                case "/stats":
                    if ("GET".equals(method)) {
                        sendJson(ex, 200, service.stats());
                    } else {
                        sendError(ex, 405, "只支持 GET");
                    }
                    break;
                default:
                    sendError(ex, 404, "未知路径: " + path + "（见 GET /）");
            }
        } catch (IllegalStateException e) {
            // 尚未加载数据集等状态错误
            sendError(ex, 409, e.getMessage());
        } catch (IllegalArgumentException | Json.JsonException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "服务器内部错误: " + e);
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", service.isLoaded() ? "ready" : "no-data");
        m.put("loaded", service.isLoaded());
        sendJson(ex, 200, m);
    }

    private void handleLoad(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        int rows = service.load(body);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("rowCount", rows);
        sendJson(ex, 200, resp);
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        IndexService.QueryResult r = service.query(body);
        List<Object> ids = new ArrayList<>(r.ids.length);
        for (int id : r.ids) {
            ids.add((long) id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ids", ids);
        resp.put("count", ids.size());
        resp.put("matchedAlive", r.matchedAlive);
        resp.put("truncated", r.truncated);
        resp.put("aliveCount", r.aliveCount);
        resp.put("totalCount", r.totalCount);
        sendJson(ex, 200, resp);
    }

    @SuppressWarnings("unchecked")
    private void handleDeleteOrRestore(HttpExchange ex, boolean deleting) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        List<Object> ids = (List<Object>) body.get("ids");
        Map<String, Object> where = null;
        if (body.get("where") instanceof Map<?, ?> w) {
            where = (Map<String, Object>) w;
        } else if (body.containsKey("where")) {
            throw new IllegalArgumentException("where 必须是表达式对象");
        }
        IndexService.ChangeResult r = deleting ? service.delete(ids, where) : service.restore(ids, where);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("action", deleting ? "deleted" : "restored");
        resp.put("changed", r.changed);
        resp.put("aliveCount", r.aliveCount);
        resp.put("totalCount", r.totalCount);
        sendJson(ex, 200, resp);
    }

    private static Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        String lenHeader = ex.getRequestHeaders().getFirst("Content-Length");
        if (lenHeader != null) {
            try {
                if (Long.parseLong(lenHeader) > MAX_BODY) {
                    throw new IllegalArgumentException("请求体超过上限 " + MAX_BODY + " 字节");
                }
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("非法 Content-Length: " + lenHeader);
            }
        }
        byte[] data;
        try (InputStream in = ex.getRequestBody()) {
            data = in.readNBytes(MAX_BODY + 1);
        }
        if (data.length > MAX_BODY) {
            throw new IllegalArgumentException("请求体超过上限 " + MAX_BODY + " 字节");
        }
        String text = new String(data, StandardCharsets.UTF_8);
        if (text.isBlank()) {
            throw new IllegalArgumentException("请求体为空，需要 JSON");
        }
        Object parsed = Json.parse(text);
        if (!(parsed instanceof Map<?, ?>)) {
            throw new IllegalArgumentException("请求体必须是 JSON 对象");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> map = (Map<String, Object>) parsed;
        return map;
    }

    private static void sendJson(HttpExchange ex, int code, Object payload) throws IOException {
        byte[] body = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, body.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(body);
        }
    }

    private static void sendError(HttpExchange ex, int code, String message) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", false);
        m.put("error", message);
        sendJson(ex, code, m);
    }

    private static int parsePort(String s) {
        try {
            int p = Integer.parseInt(s);
            if (p < 1 || p > 65535) {
                throw new NumberFormatException();
            }
            return p;
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("非法端口: " + s);
        }
    }

    private static String requireArgValue(String[] args, int i, String flag) {
        if (i >= args.length) {
            throw new IllegalArgumentException(flag + " 需要一个参数");
        }
        return args[i];
    }

    private static Map<String, Object> apiHelp() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("service", "multi-dimensional-bitmap-index");
        m.put("note", "纯 JDK HttpServer 实现；行 ID 为加载顺序下标的 0 基行号，删除为软删除且不改变行 ID；NOT 仅在存活行全集内取补");
        Map<String, String> endpoints = new LinkedHashMap<>();
        endpoints.put("GET /", "本说明");
        endpoints.put("GET /health", "健康检查 / 加载状态");
        endpoints.put("POST /load", "加载数据集 {csv|csvPath|columns+rows}（整体替换，行 ID 重新按新数据顺序编号）");
        endpoints.put("POST /query", "{where: 表达式, limit?: n} -> 命中行 ids");
        endpoints.put("POST /delete", "{ids:[...]} 或 {where: 表达式} 软删除");
        endpoints.put("POST /restore", "{ids:[...]} 或 {where: 表达式} 恢复");
        endpoints.put("GET /stats", "索引空间统计（未压缩 long[] 与 RLE 压缩）");
        m.put("endpoints", endpoints);
        Map<String, String> expr = new LinkedHashMap<>();
        expr.put("eq", "{\"op\":\"eq\",\"col\":\"city\",\"value\":\"BJ\"}");
        expr.put("in", "{\"op\":\"in\",\"col\":\"city\",\"values\":[\"BJ\",\"SH\"]}");
        expr.put("and", "{\"op\":\"and\",\"args\":[...]}");
        expr.put("or", "{\"op\":\"or\",\"args\":[...]}");
        expr.put("not", "{\"op\":\"not\",\"arg\":{...}}  // = alive AND NOT arg");
        expr.put("alive", "{\"op\":\"alive\"}  // 当前存活全集");
        m.put("expressions", expr);
        return m;
    }
}
