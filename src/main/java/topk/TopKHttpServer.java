package topk;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 基于 JDK 内置 {@code com.sun.net.httpserver.HttpServer} 的纯后端 HTTP 服务。
 *
 * <pre>
 * GET  /health
 * POST /groups/{group}/events     插入事件   {"eventId","itemId","delta"(整数,可负),"ts"(可选,默认当前墙钟)}
 * POST /groups/{group}/retract    撤回事件   {"eventId","ts"(可选)}
 * GET  /groups/{group}/topk?k=10&ts=12345     ts 可选，给出则先推进窗口水位
 * GET  /groups/{group}/snapshot              排障/可观测性：完整排序与内部计数
 * </pre>
 *
 * <p>时间戳均为逻辑毫秒时间，由调用方提供，便于确定性测试；缺省退回墙钟时间。
 */
public final class TopKHttpServer {

    private static final int MAX_BODY_BYTES = 1 << 20; // 1 MiB

    private final TopKService service;
    private final HttpServer server;
    private final int port;

    public TopKHttpServer(TopKService service, int port) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.port = server.getAddress().getPort();
        server.createContext("/", new RootHandler());
        AtomicInteger n = new AtomicInteger();
        ThreadFactory tf = r -> {
            Thread t = new Thread(r, "topk-http-" + n.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
        server.setExecutor(Executors.newFixedThreadPool(8, tf));
    }

    public int port() {
        return port;
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(getArg(args, 0, envOr("TOPK_PORT", "8080")));
        long windowMs = Long.parseLong(getArg(args, 1, envOr("TOPK_WINDOW_MS", "10000")));
        if (windowMs <= 0) {
            throw new IllegalArgumentException("windowMs 必须为正数: " + windowMs);
        }
        TopKHttpServer srv = new TopKHttpServer(new TopKService(windowMs), port);
        srv.start();
        System.out.println("TopK 服务已启动: http://localhost:" + srv.port()
                + "  窗口 windowMs=" + windowMs);
        System.out.println("接口: POST /groups/{g}/events | POST /groups/{g}/retract "
                + "| GET /groups/{g}/topk?k= | GET /health");
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("正在关闭 TopK 服务 ...");
            srv.stop();
        }));
    }

    private static String getArg(String[] args, int i, String def) {
        return args.length > i ? args[i] : def;
    }

    private static String envOr(String name, String def) {
        String v = System.getenv(name);
        return (v == null || v.isEmpty()) ? def : v;
    }

    // ---------------- 路由与处理 ----------------

    private final class RootHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) {
            try {
                route(ex);
            } catch (Exception e) {
                try {
                    Map<String, Object> err = new LinkedHashMap<>();
                    err.put("error", "内部错误: " + e);
                    writeJson(ex, 500, err);
                } catch (Exception suppressed) {
                    // 响应已无法写出，放弃
                }
            } finally {
                ex.close();
            }
        }

        private void route(HttpExchange ex) throws IOException {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();
            String[] seg = splitPath(path);

            if (seg.length == 1 && seg[0].equals("health")) {
                if (!requireMethod(ex, method, "GET")) {
                    return;
                }
                Map<String, Object> h = new LinkedHashMap<>();
                h.put("status", "ok");
                h.put("windowMs", service.windowMs());
                h.put("groups", service.groupCount());
                writeJson(ex, 200, h);
                return;
            }

            if (seg.length == 3 && seg[0].equals("groups")) {
                String group = urlDecode(seg[1]);
                if (group.isEmpty()) {
                    sendError(ex, 400, "group 不能为空");
                    return;
                }
                String action = seg[2];
                switch (action) {
                    case "events":
                        if (!requireMethod(ex, method, "POST")) {
                            return;
                        }
                        handleInsert(ex, group);
                        return;
                    case "retract":
                        if (!requireMethod(ex, method, "POST")) {
                            return;
                        }
                        handleRetract(ex, group);
                        return;
                    case "topk":
                        if (!requireMethod(ex, method, "GET")) {
                            return;
                        }
                        handleTopK(ex, group);
                        return;
                    case "snapshot":
                        if (!requireMethod(ex, method, "GET")) {
                            return;
                        }
                        writeJson(ex, 200, service.snapshot(group));
                        return;
                    default:
                        break;
                }
            }
            sendError(ex, 404, "未找到路径: " + method + " " + path);
        }

        private void handleInsert(HttpExchange ex, String group) throws IOException {
            Map<String, Object> body;
            try {
                body = Json.parseObject(readBody(ex));
            } catch (BadRequestException | IllegalArgumentException bre) {
                sendError(ex, 400, "请求体不是合法 JSON 对象: " + bre.getMessage());
                return;
            }
            String eventId;
            String itemId;
            long delta;
            long ts;
            try {
                eventId = requireString(body, "eventId");
                itemId = requireString(body, "itemId");
                delta = requireLong(body, "delta");
                ts = optionalLong(body, "ts", System.currentTimeMillis());
            } catch (BadRequestException bre) {
                sendError(ex, 400, bre.getMessage());
                return;
            }
            Event e = new Event(eventId, itemId, delta, ts);

            GroupState.InsertStatus st = service.insert(group, e, ts);
            GroupState g = service.group(group);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("status", st.name());
            resp.put("eventId", eventId);
            resp.put("itemId", itemId);
            resp.put("delta", delta);
            resp.put("ts", ts);
            synchronized (g) {
                resp.put("watermark", g.watermark());
                resp.put("activeEvents", g.activeEventCount());
                resp.put("activeItems", g.activeItemCount());
            }
            int code;
            switch (st) {
                case DUPLICATE:
                    code = 409;
                    resp.put("error", "eventId 在窗口内已存在（或在撤回保留期内）");
                    break;
                case LATE:
                    code = 422;
                    resp.put("error", "事件时间已滑出窗口（迟到事件），未被接受");
                    break;
                default:
                    code = 200;
            }
            writeJson(ex, code, resp);
        }

        private void handleRetract(HttpExchange ex, String group) throws IOException {
            Map<String, Object> body;
            try {
                body = Json.parseObject(readBody(ex));
            } catch (BadRequestException | IllegalArgumentException bre) {
                sendError(ex, 400, "请求体不是合法 JSON 对象: " + bre.getMessage());
                return;
            }
            String eventId;
            long ts;
            try {
                eventId = requireString(body, "eventId");
                ts = optionalLong(body, "ts", System.currentTimeMillis());
            } catch (BadRequestException bre) {
                sendError(ex, 400, bre.getMessage());
                return;
            }

            GroupState.RetractStatus st = service.retract(group, eventId, ts);
            GroupState g = service.group(group);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("status", st.name());
            resp.put("eventId", eventId);
            resp.put("ts", ts);
            synchronized (g) {
                resp.put("watermark", g.watermark());
                resp.put("activeEvents", g.activeEventCount());
                resp.put("activeItems", g.activeItemCount());
            }
            int code;
            switch (st) {
                case EVENT_UNKNOWN:
                    code = 404;
                    resp.put("error", "事件不存在或已过期滑出窗口，无法撤回");
                    break;
                case ALREADY_RETRACTED:
                    code = 200; // 幂等
                    break;
                default:
                    code = 200;
            }
            writeJson(ex, code, resp);
        }

        private void handleTopK(HttpExchange ex, String group) throws IOException {
            Map<String, String> q = parseQuery(ex.getRequestURI().getRawQuery());
            final int k;
            try {
                String kStr = q.get("k");
                if (kStr == null) {
                    throw new BadRequestException("缺少查询参数 k");
                }
                k = Integer.parseInt(kStr);
                if (k < 0) {
                    throw new BadRequestException("k 不能为负");
                }
            } catch (NumberFormatException nfe) {
                sendError(ex, 400, "k 必须为非负整数");
                return;
            } catch (BadRequestException bre) {
                sendError(ex, 400, bre.getMessage());
                return;
            }
            Long ts = null;
            String tsStr = q.get("ts");
            if (tsStr != null) {
                try {
                    ts = Long.parseLong(tsStr);
                } catch (NumberFormatException nfe) {
                    sendError(ex, 400, "ts 必须为整数毫秒时间戳");
                    return;
                }
            }

            List<GroupState.Row> rows = service.topK(group, k, ts);
            GroupState g = service.group(group);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("group", group);
            resp.put("k", k);
            resp.put("windowMs", service.windowMs());
            resp.put("count", rows.size());
            List<Map<String, Object>> items = new ArrayList<>();
            int rank = 1;
            for (GroupState.Row r : rows) {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("rank", rank++);
                row.put("itemId", r.itemId);
                row.put("score", r.score);
                items.add(row);
            }
            resp.put("items", items);
            synchronized (g) {
                resp.put("watermark", g.watermark());
                resp.put("windowStart", g.watermark() == null ? null : g.watermark() - g.windowMs());
            }
            writeJson(ex, 200, resp);
        }
    }

    // ---------------- 工具方法 ----------------

    private static String[] splitPath(String path) {
        List<String> out = new ArrayList<>();
        for (String part : path.split("/")) {
            if (!part.isEmpty()) {
                out.add(part);
            }
        }
        return out.toArray(new String[0]);
    }

    private static String urlDecode(String s) {
        return URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    /** 方法不符时返回 false（已写出 405 响应，调用方应终止路由）。 */
    private static boolean requireMethod(HttpExchange ex, String actual, String expected) throws IOException {
        if (!actual.equals(expected)) {
            sendError(ex, 405, "仅支持 " + expected + "，收到 " + actual);
            return false;
        }
        return true;
    }

    private static void sendError(HttpExchange ex, int code, String msg) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", msg);
        err.put("status", code);
        writeJson(ex, code, err);
    }

    private static void writeJson(HttpExchange ex, int code, Object payload) throws IOException {
        byte[] bytes = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        Headers h = ex.getResponseHeaders();
        h.set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream is = ex.getRequestBody();
             ByteArrayOutputStream buf = new ByteArrayOutputStream()) {
            byte[] tmp = new byte[8192];
            int total = 0;
            int n;
            while ((n = is.read(tmp)) != -1) {
                total += n;
                if (total > MAX_BODY_BYTES) {
                    throw new BadRequestException("请求体超过 " + MAX_BODY_BYTES + " 字节上限");
                }
                buf.write(tmp, 0, n);
            }
            return buf.toString(StandardCharsets.UTF_8);
        }
    }

    private static Map<String, String> parseQuery(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String val = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(urlDecode(key), urlDecode(val));
        }
        return out;
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new BadRequestException("字段 " + key + " 必须为非空字符串");
        }
        return (String) v;
    }

    private static long requireLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (d != Math.rint(d)) {
                throw new BadRequestException("字段 " + key + " 必须为整数");
            }
            return ((Number) v).longValue();
        }
        throw new BadRequestException("字段 " + key + " 必须为整数");
    }

    private static long optionalLong(Map<String, Object> body, String key, long def) {
        Object v = body.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (d != Math.rint(d)) {
                throw new BadRequestException("字段 " + key + " 必须为整数毫秒时间戳");
            }
            return ((Number) v).longValue();
        }
        throw new BadRequestException("字段 " + key + " 必须为整数毫秒时间戳");
    }

    /** 请求本身可预期的错误（4xx）。 */
    private static final class BadRequestException extends RuntimeException {
        BadRequestException(String msg) {
            super(msg);
        }
    }
}
