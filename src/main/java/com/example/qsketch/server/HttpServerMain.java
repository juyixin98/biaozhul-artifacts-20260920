package com.example.qsketch.server;

import com.example.qsketch.Json;
import com.example.qsketch.QDigest;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * HTTP service for mergeable quantile summaries, built only on
 * {@code com.sun.net.httpserver.HttpServer} (JDK). No frameworks, no
 * third-party runtime dependencies.
 *
 * <h2>Endpoints</h2>
 * <pre>
 * POST   /summaries                {id, eps, universe}
 * GET    /summaries                list
 * GET    /summaries/{id}           params, count, nodeCount, bounds
 * POST   /summaries/{id}/values    {values:[...]}                (insert samples)
 * GET    /summaries/{id}/quantile?q=0.5
 * GET    /summaries/{id}/rank?x=42
 * POST   /summaries/merge          {target, sources:[...]}
 * GET    /summaries/{id}/serialize -> {data: base64 binary}
 * POST   /summaries/deserialize    {id, data} -> creates a summary
 * DELETE /summaries/{id}
 * </pre>
 */
public final class HttpServerMain {

    private static final int MAX_BODY = 16 * 1024 * 1024;

    private final SummaryStore store = new SummaryStore();
    private final HttpServer server;

    public HttpServerMain(int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::route);
        ThreadPoolExecutor pool =
                (ThreadPoolExecutor) Executors.newFixedThreadPool(8, r -> {
                    Thread t = new Thread(r, "qsketch-http");
                    t.setDaemon(true);
                    return t;
                });
        server.setExecutor(pool);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(1);
    }

    public static void main(String[] args) throws IOException {
        int port = 8080;
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            }
        }
        HttpServerMain app = new HttpServerMain(port);
        app.start();
        System.out.println("qsketch listening on http://localhost:" + app.port());
    }

    // ---- routing -------------------------------------------------------

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            String[] parts = splitPath(path);

            if (parts.length == 1 && parts[0].equals("summaries")) {
                if (method.equals("POST")) {
                    handleCreate(ex);
                } else if (method.equals("GET")) {
                    handleList(ex);
                } else {
                    methodNotAllowed(ex);
                }
            } else if (parts.length == 2 && parts[0].equals("summaries")
                    && parts[1].equals("merge") && method.equals("POST")) {
                handleMerge(ex);
            } else if (parts.length == 2 && parts[0].equals("summaries")
                    && parts[1].equals("deserialize") && method.equals("POST")) {
                handleDeserialize(ex);
            } else if (parts.length == 2 && parts[0].equals("summaries")) {
                if (method.equals("GET")) {
                    handleGet(ex, parts[1]);
                } else if (method.equals("DELETE")) {
                    handleDelete(ex, parts[1]);
                } else {
                    methodNotAllowed(ex);
                }
            } else if (parts.length == 3 && parts[0].equals("summaries")) {
                String id = parts[1];
                switch (parts[2]) {
                    case "values":
                        requirePost(method, ex);
                        handleValues(ex, id);
                        break;
                    case "quantile":
                        requireGet(method, ex);
                        handleQuantile(ex, id);
                        break;
                    case "rank":
                        requireGet(method, ex);
                        handleRank(ex, id);
                        break;
                    case "serialize":
                        requireGet(method, ex);
                        handleSerialize(ex, id);
                        break;
                    default:
                        notFound(ex);
                }
            } else {
                notFound(ex);
            }
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "internal error: " + e.getMessage());
        }
    }

    private static String[] splitPath(String path) {
        String p = path.startsWith("/") ? path.substring(1) : path;
        if (p.endsWith("/")) {
            p = p.substring(0, p.length() - 1);
        }
        return p.isEmpty() ? new String[0] : p.split("/");
    }

    private void requirePost(String method, HttpExchange ex) throws IOException {
        if (!method.equals("POST")) {
            methodNotAllowed(ex);
        }
    }

    private void requireGet(String method, HttpExchange ex) throws IOException {
        if (!method.equals("GET")) {
            methodNotAllowed(ex);
        }
    }

    // ---- handlers ------------------------------------------------------

    private void handleCreate(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        String id = requireString(body, "id");
        double eps = requireDouble(body, "eps");
        int universe = (int) requireLong(body, "universe");
        try {
            QDigest d = store.create(id, eps, universe);
            sendJson(ex, 201, summaryView(id, d));
        } catch (IllegalStateException e) {
            sendError(ex, 409, e.getMessage());
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        }
    }

    private void handleList(HttpExchange ex) throws IOException {
        List<Object> out = new ArrayList<>();
        for (String id : store.ids()) {
            out.add(summaryView(id, store.get(id)));
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("summaries", out);
        sendJson(ex, 200, resp);
    }

    private void handleGet(HttpExchange ex, String id) throws IOException {
        if (!store.exists(id)) {
            notFound(ex);
            return;
        }
        sendJson(ex, 200, summaryView(id, store.get(id)));
    }

    private void handleDelete(HttpExchange ex, String id) throws IOException {
        if (!store.exists(id)) {
            notFound(ex);
            return;
        }
        store.delete(id);
        sendJson(ex, 200, mapOf("deleted", id));
    }

    private void handleValues(HttpExchange ex, String id) throws IOException {
        QDigest d;
        try {
            d = store.get(id);
        } catch (IllegalArgumentException e) {
            notFound(ex);
            return;
        }
        Map<String, Object> body = readJsonObject(ex);
        Object raw = body.get("values");
        if (!(raw instanceof List)) {
            sendError(ex, 400, "field 'values' must be an array of integers");
            return;
        }
        long[] values = toLongArray((List<?>) raw);
        synchronized (d) {
            d.insertAll(values);
        }
        sendJson(ex, 200, summaryView(id, d));
    }

    private void handleQuantile(HttpExchange ex, String id) throws IOException {
        QDigest d;
        try {
            d = store.get(id);
        } catch (IllegalArgumentException e) {
            notFound(ex);
            return;
        }
        double q;
        try {
            q = Double.parseDouble(queryParam(ex, "q"));
        } catch (RuntimeException e) {
            sendError(ex, 400, "missing or invalid query parameter 'q'");
            return;
        }
        if (!(q >= 0 && q <= 1)) {
            sendError(ex, 400, "q must be in [0,1]");
            return;
        }
        long v;
        long[] bounds;
        synchronized (d) {
            if (d.count() == 0) {
                sendError(ex, 409, "summary is empty");
                return;
            }
            v = d.quantile(q);
            bounds = d.rankBounds(v);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("id", id);
        resp.put("q", q);
        resp.put("value", v);
        resp.put("rankLowerBound", bounds[0]);
        resp.put("rankUpperBound", bounds[1]);
        resp.put("targetRank", Math.max(1, Math.round(q * d.count())));
        resp.put("maxRankError", Math.ceil(d.eps() * d.count()) + Math.ceil(log2(d.universe())));
        sendJson(ex, 200, resp);
    }

    private void handleRank(HttpExchange ex, String id) throws IOException {
        QDigest d;
        try {
            d = store.get(id);
        } catch (IllegalArgumentException e) {
            notFound(ex);
            return;
        }
        long x;
        try {
            x = Long.parseLong(queryParam(ex, "x"));
        } catch (RuntimeException e) {
            sendError(ex, 400, "missing or invalid query parameter 'x'");
            return;
        }
        long[] bounds;
        double mid;
        synchronized (d) {
            if (x < 0 || x >= d.universe()) {
                sendError(ex, 400, "x outside universe");
                return;
            }
            bounds = d.rankBounds(x);
            mid = d.estimatedRank(x);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("id", id);
        resp.put("x", x);
        resp.put("rankLowerBound", bounds[0]);
        resp.put("rankUpperBound", bounds[1]);
        resp.put("estimatedRank", mid);
        resp.put("maxRankError", Math.ceil(d.eps() * d.count()) + Math.ceil(log2(d.universe())));
        sendJson(ex, 200, resp);
    }

    private void handleMerge(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        String target = requireString(body, "target");
        Object raw = body.get("sources");
        if (!(raw instanceof List) || ((List<?>) raw).isEmpty()) {
            sendError(ex, 400, "field 'sources' must be a non-empty array of ids");
            return;
        }
        List<String> sources = new ArrayList<>();
        for (Object o : (List<?>) raw) {
            sources.add(String.valueOf(o));
        }
        try {
            QDigest d = store.mergeInto(target, sources);
            sendJson(ex, 200, summaryView(target, d));
        } catch (IllegalArgumentException e) {
            // includes parameter-incompatibility and missing summaries
            sendError(ex, 409, e.getMessage());
        }
    }

    private void handleSerialize(HttpExchange ex, String id) throws IOException {
        QDigest d;
        try {
            d = store.get(id);
        } catch (IllegalArgumentException e) {
            notFound(ex);
            return;
        }
        byte[] data;
        synchronized (d) {
            data = d.toByteArray();
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("id", id);
        resp.put("encoding", "base64");
        resp.put("format", "qdigest-v1");
        resp.put("bytes", data.length);
        resp.put("data", Base64.getEncoder().encodeToString(data));
        sendJson(ex, 200, resp);
    }

    private void handleDeserialize(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        String id = requireString(body, "id");
        String b64 = requireString(body, "data");
        QDigest d;
        try {
            byte[] raw = Base64.getDecoder().decode(b64);
            d = QDigest.fromByteArray(raw);
        } catch (IllegalArgumentException | IOException e) {
            sendError(ex, 400, "invalid serialized summary: " + e.getMessage());
            return;
        }
        if (store.exists(id)) {
            sendError(ex, 409, "summary '" + id + "' already exists");
            return;
        }
        store.put(id, d);
        sendJson(ex, 201, summaryView(id, d));
    }

    // ---- helpers -------------------------------------------------------

    private Map<String, Object> summaryView(String id, QDigest d) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("eps", d.eps());
        m.put("universe", d.universe());
        m.put("count", d.count());
        m.put("nodeCount", d.nodeCount());
        m.put("theoreticalNodeBound", d.nodeCountBound());
        return m;
    }

    private static Map<String, Object> mapOf(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new IllegalArgumentException("field '" + key + "' must be a non-empty string");
        }
        return (String) v;
    }

    private static double requireDouble(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v instanceof Number) {
            return ((Number) v).doubleValue();
        }
        throw new IllegalArgumentException("field '" + key + "' must be a number");
    }

    private static long requireLong(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        throw new IllegalArgumentException("field '" + key + "' must be an integer");
    }

    private static long[] toLongArray(List<?> list) {
        long[] out = new long[list.size()];
        for (int i = 0; i < list.size(); i++) {
            Object o = list.get(i);
            if (!(o instanceof Number)) {
                throw new IllegalArgumentException("values must all be integers");
            }
            out[i] = ((Number) o).longValue();
        }
        return out;
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            throw new IllegalArgumentException();
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(name)) {
                return java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        throw new IllegalArgumentException();
    }

    private static double log2(int x) {
        return 32 - Integer.numberOfLeadingZeros(Math.max(1, x - 1));
    }

    private Map<String, Object> readJsonObject(HttpExchange ex) {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new IllegalArgumentException("POST required");
        }
        long len = ex.getRequestHeaders().getFirst("Content-Length") == null
                ? -1
                : Long.parseLong(ex.getRequestHeaders().getFirst("Content-Length"));
        if (len > MAX_BODY) {
            throw new IllegalArgumentException("request body too large");
        }
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int r;
            int total = 0;
            while ((r = ex.getRequestBody().read(buf)) != -1) {
                total += r;
                if (total > MAX_BODY) {
                    throw new IllegalArgumentException("request body too large");
                }
                bos.write(buf, 0, r);
            }
            String text = bos.toString(StandardCharsets.UTF_8);
            return Json.parseObject(text);
        } catch (IOException | RuntimeException e) {
            throw new IllegalArgumentException("invalid request body: " + e.getMessage());
        }
    }

    private void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private void sendError(HttpExchange ex, int status, String message) throws IOException {
        sendJson(ex, status, mapOf("error", message));
    }

    private void notFound(HttpExchange ex) throws IOException {
        sendError(ex, 404, "not found");
    }

    private void methodNotAllowed(HttpExchange ex) throws IOException {
        sendError(ex, 405, "method not allowed");
    }
}
