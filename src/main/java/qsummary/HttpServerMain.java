package qsummary;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * HTTP front end for the streaming quantile summary service.
 *
 * <p>Built only on the JDK ({@code com.sun.net.httpserver.HttpServer}); there
 * are no third-party dependencies. See README.md for the full API and
 * {@code examples/requests.sh} for runnable requests.
 */
public final class HttpServerMain {

    private static final long MAX_BODY_BYTES = 64L * 1024 * 1024;
    private static final double DEFAULT_EPSILON = 0.01d;

    private final SummaryStore store = new SummaryStore();

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String host = "0.0.0.0";
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(args[++i]);
                case "--host" -> host = args[++i];
                case "--help" -> {
                    System.out.println("Usage: qsummary-server [--host 0.0.0.0] [--port 8080]");
                    return;
                }
                default -> throw new IllegalArgumentException("unknown argument: " + args[i]);
            }
        }
        new HttpServerMain().start(host, port);
    }

    HttpServer start(String host, int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(host, port), 0);
        server.createContext("/", this::route);
        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newCachedThreadPool(r -> {
            Thread t = new Thread(r, "qsummary-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.start();
        InetSocketAddress addr = server.getAddress();
        System.out.printf("quantile summary service listening on http://%s:%d%n",
                addr.getHostString(), addr.getPort());
        System.out.println("epsilon configurable per summary; health: GET /healthz");
        return server;
    }

    // ------------------------------------------------------------------
    // Routing
    // ------------------------------------------------------------------

    private void route(HttpExchange ex) {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/healthz" -> {
                    requireGet(method);
                    sendJson(ex, 200, Map.of("status", "ok"));
                }
                case "/v1/summaries" -> {
                    requireGet(method);
                    listSummaries(ex);
                }
                case "/v1/merge" -> {
                    requirePost(method);
                    merge(ex);
                }
                default -> routeSummary(ex, path, method);
            }
        } catch (HttpStatusException e) {
            sendError(ex, e.status, e.getMessage());
        } catch (BadRequestException e) {
            sendError(ex, 400, e.getMessage());
        } catch (NotFoundException e) {
            sendError(ex, 404, e.getMessage());
        } catch (IncompatibleSummaryException e) {
            // 422 mirrors the semantics: the summaries cannot be combined.
            sendError(ex, 422, e.getMessage());
        } catch (IllegalStateException e) {
            // E.g. querying an empty summary: legal object, unusable state.
            sendError(ex, 409, e.getMessage());
        } catch (Exception e) {
            e.printStackTrace(System.err);
            sendError(ex, 500, "internal error: " + e);
        }
    }

    private void routeSummary(HttpExchange ex, String path, String method) {
        // /v1/summaries/{id}[/observations[/stream]|/quantile|/rank|/snapshot]
        String[] parts = path.split("/");
        // ["", "v1", "summaries", id, ...]
        if (parts.length < 4 || !"v1".equals(parts[1]) || !"summaries".equals(parts[2])) {
            throw new NotFoundException("not found: " + path);
        }
        String id = parts[3];
        SummaryStore.validateId(id);

        if (parts.length == 4) {
            switch (method) {
                case "PUT" -> create(ex, id);
                case "GET" -> metadata(ex, id);
                case "DELETE" -> delete(ex, id);
                default -> throw new HttpStatusException(405, "method not allowed");
            }
            return;
        }
        String sub = parts[4];
        switch (sub) {
            case "observations" -> {
                if (parts.length == 5 && "POST".equals(method)) {
                    addObservations(ex, id);
                } else if (parts.length == 6 && "stream".equals(parts[5]) && "POST".equals(method)) {
                    addObservationsStream(ex, id);
                } else {
                    throw new HttpStatusException(404, "not found: " + path);
                }
            }
            case "quantile" -> {
                requireGet(method);
                quantile(ex, id);
            }
            case "rank" -> {
                requireGet(method);
                rank(ex, id);
            }
            case "snapshot" -> {
                if ("GET".equals(method)) {
                    getSnapshot(ex, id);
                } else if ("PUT".equals(method)) {
                    putSnapshot(ex, id);
                } else {
                    throw new HttpStatusException(405, "method not allowed");
                }
            }
            default -> throw new NotFoundException("not found: " + path);
        }
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    private void create(HttpExchange ex, String id) {
        double epsilon = DEFAULT_EPSILON;
        byte[] body = readBody(ex);
        if (body.length > 0) {
            Map<String, Object> m = Json.asObject(Json.parse(new String(body, StandardCharsets.UTF_8)));
            if (m.containsKey("epsilon")) {
                epsilon = Json.getDouble(m, "epsilon");
            }
        }
        // Constructor validates the range.
        GKQuantileSummary s = store.create(id, epsilon);
        sendJson(ex, 201, store.describe(id, s));
    }

    private void listSummaries(HttpExchange ex) {
        List<Object> all = new ArrayList<>();
        for (String id : store.ids()) {
            all.add(store.describe(id, store.get(id)));
        }
        sendJson(ex, 200, Map.of("summaries", all));
    }

    private void metadata(HttpExchange ex, String id) {
        sendJson(ex, 200, store.describe(id, store.get(id)));
    }

    private void delete(HttpExchange ex, String id) {
        boolean removed = store.delete(id);
        if (!removed) {
            throw new NotFoundException("no summary '" + id + "'");
        }
        sendJson(ex, 200, Map.of("deleted", id));
    }

    private void addObservations(HttpExchange ex, String id) {
        GKQuantileSummary s = store.get(id);
        Map<String, Object> m = Json.asObject(Json.parse(readBodyString(ex)));
        Object raw = m.get("values");
        if (!(raw instanceof List<?> list)) {
            throw new BadRequestException("'values' must be an array of numbers");
        }
        if (list.size() > 5_000_000) {
            throw new BadRequestException("too many values in one request (max 5,000,000)");
        }
        for (Object o : list) {
            if (!(o instanceof Number n)) {
                throw new BadRequestException("'values' must contain only numbers");
            }
            s.insert(n.doubleValue());
        }
        sendJson(ex, 200, store.describe(id, s));
    }

    private void addObservationsStream(HttpExchange ex, String id) {
        GKQuantileSummary s = store.get(id);
        String body = readBodyString(ex);
        long added = 0;
        // Whitespace separated numbers (spaces, tabs, newlines); one value
        // per line works too.
        int start = -1;
        for (int i = 0; i <= body.length(); i++) {
            boolean endToken = i == body.length() || Character.isWhitespace(body.charAt(i));
            if (!endToken) {
                if (start < 0) start = i;
            } else if (start >= 0) {
                String tok = body.substring(start, i);
                double v;
                try {
                    v = Double.parseDouble(tok);
                } catch (NumberFormatException e) {
                    throw new BadRequestException("not a number: " + tok);
                }
                s.insert(v);
                added++;
                start = -1;
            }
        }
        Map<String, Object> resp = new LinkedHashMap<>(store.describe(id, s));
        resp.put("added", added);
        sendJson(ex, 200, resp);
    }

    private void quantile(HttpExchange ex, String id) {
        GKQuantileSummary s = store.get(id);
        String qs = queryParam(ex, "q");
        String qsMulti = queryParam(ex, "qs");
        if (qs == null && qsMulti == null) {
            throw new BadRequestException("query parameter q (e.g. ?q=0.5) is required");
        }
        List<String> tokens = new ArrayList<>();
        if (qs != null) tokens.add(qs);
        if (qsMulti != null) {
            for (String t : qsMulti.split(",")) {
                if (!t.isBlank()) tokens.add(t.trim());
            }
        }
        List<Object> results = new ArrayList<>(tokens.size());
        long bound = SummaryStore.errorBound(s.epsilon(), s.count());
        for (String tok : tokens) {
            double q;
            try {
                q = Double.parseDouble(tok);
            } catch (NumberFormatException e) {
                throw new BadRequestException("q not a number: " + tok);
            }
            double value = s.quantile(q);
            long rank = s.rankOf(value);
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("q", q);
            entry.put("value", value);
            entry.put("estimatedRank", rank);
            entry.put("errorBoundRank", bound);
            results.add(entry);
        }
        Map<String, Object> resp = new LinkedHashMap<>(store.describe(id, s));
        resp.put("results", results);
        sendJson(ex, 200, resp);
    }

    private void rank(HttpExchange ex, String id) {
        GKQuantileSummary s = store.get(id);
        String valueParam = queryParam(ex, "value");
        if (valueParam == null) {
            throw new BadRequestException("query parameter value (e.g. ?value=42) is required");
        }
        double value;
        try {
            value = Double.parseDouble(valueParam);
        } catch (NumberFormatException e) {
            throw new BadRequestException("value not a number: " + valueParam);
        }
        long rank = s.rankOf(value);
        double cdf = s.count() == 0 ? 0d : (double) rank / s.count();
        Map<String, Object> resp = new LinkedHashMap<>(store.describe(id, s));
        resp.put("value", value);
        resp.put("estimatedRank", rank);
        resp.put("cdf", cdf);
        resp.put("errorBoundRank", SummaryStore.errorBound(s.epsilon(), s.count()));
        sendJson(ex, 200, resp);
    }

    /**
     * Merge shards. Body:
     * {"id":"out", "sources":["shard1","shard2", ...]}
     * and/or {"id":"out", "snapshots":[ <snapshot>, ... ]}.
     * All participants must carry identical epsilon/algorithm/order, else 422.
     * The target id must not already exist.
     */
    @SuppressWarnings("unchecked")
    private void merge(HttpExchange ex) {
        Map<String, Object> m = Json.asObject(Json.parse(readBodyString(ex)));
        String id = Json.getString(m, "id");
        SummaryStore.validateId(id);

        List<GKQuantileSummary> parts = new ArrayList<>();
        Object sources = m.get("sources");
        if (sources != null) {
            if (!(sources instanceof List<?> list)) {
                throw new BadRequestException("'sources' must be an array of summary ids");
            }
            for (Object o : list) {
                if (!(o instanceof String sid)) {
                    throw new BadRequestException("'sources' entries must be strings");
                }
                if (sid.equals(id)) {
                    throw new BadRequestException("merge target id must not appear in sources");
                }
                parts.add(store.get(sid));
            }
        }
        Object snapshots = m.get("snapshots");
        if (snapshots != null) {
            if (!(snapshots instanceof List<?> list)) {
                throw new BadRequestException("'snapshots' must be an array");
            }
            for (Object o : list) {
                parts.add(GKQuantileSummary.fromSnapshot(o));
            }
        }
        if (parts.isEmpty()) {
            throw new BadRequestException("merge needs at least one source or snapshot");
        }

        // Parameter compatibility check up front (clear error before touching
        // the store): every pair must agree on epsilon.
        double eps = parts.get(0).epsilon();
        for (int i = 1; i < parts.size(); i++) {
            if (Math.abs(parts.get(i).epsilon() - eps) > 1e-15) {
                throw new IncompatibleSummaryException(String.format(
                        "cannot merge: epsilon mismatch at position %d (%.6g vs %.6g)",
                        i, parts.get(i).epsilon(), eps));
            }
        }

        GKQuantileSummary acc = parts.get(0);
        for (int i = 1; i < parts.size(); i++) {
            acc = GKQuantileSummary.merge(acc, parts.get(i));
        }
        // register() rejects an already existing target id; summaries are
        // immutable through merge (a new id is produced).
        store.register(id, acc);

        Map<String, Object> resp = new LinkedHashMap<>(store.describe(id, acc));
        resp.put("mergedFrom", sources);
        resp.put("snapshotsMerged", snapshots == null ? 0 : ((List<?>) snapshots).size());
        sendJson(ex, 201, resp);
    }


    private void getSnapshot(HttpExchange ex, String id) {
        GKQuantileSummary s = store.get(id);
        Map<String, Object> resp = new LinkedHashMap<>(store.describe(id, s));
        resp.put("snapshot", s.toSnapshot());
        sendJson(ex, 200, resp);
    }

    private void putSnapshot(HttpExchange ex, String id) {
        Map<String, Object> m = Json.asObject(Json.parse(readBodyString(ex)));
        Object snap = m.containsKey("snapshot") ? m.get("snapshot") : m;
        // fromSnapshot validates version/order/epsilon and tuple integrity.
        GKQuantileSummary s = GKQuantileSummary.fromSnapshot(snap);
        SummaryStore.validateId(id);
        store.register(id, s);
        sendJson(ex, 201, store.describe(id, s));
    }

    // ------------------------------------------------------------------
    // HTTP plumbing
    // ------------------------------------------------------------------

    private static void requireGet(String method) {
        if (!"GET".equals(method)) {
            throw new HttpStatusException(405, "method not allowed");
        }
    }

    private static void requirePost(String method) {
        if (!"POST".equals(method)) {
            throw new HttpStatusException(405, "method not allowed");
        }
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq < 0) continue;
            if (java.net.URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8)
                    .equals(name)) {
                return java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static byte[] readBody(HttpExchange ex) {
        try {
            long len = ex.getRequestHeaders().getFirst("Content-Length") == null
                    ? -1
                    : Long.parseLong(ex.getRequestHeaders().getFirst("Content-Length"));
            if (len > MAX_BODY_BYTES) {
                throw new BadRequestException("request body too large (limit 64 MiB)");
            }
            byte[] bytes = ex.getRequestBody().readAllBytes();
            if (bytes.length > MAX_BODY_BYTES) {
                throw new BadRequestException("request body too large (limit 64 MiB)");
            }
            return bytes;
        } catch (IOException e) {
            throw new BadRequestException("cannot read body: " + e.getMessage());
        }
    }

    private static String readBodyString(HttpExchange ex) {
        return new String(readBody(ex), StandardCharsets.UTF_8);
    }

    private static void sendJson(HttpExchange ex, int status, Object body) {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException e) {
            // Client likely gone; nothing actionable.
            System.err.println("failed to send response: " + e.getMessage());
        } finally {
            ex.close();
        }
    }

    private static void sendError(HttpExchange ex, int status, String message) {
        sendJson(ex, status, Map.of("error", message, "status", status));
    }

    /** Carrier for explicit HTTP status codes in the routing layer. */
    private static final class HttpStatusException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        final int status;
        HttpStatusException(int status, String message) {
            super(message);
            this.status = status;
        }
    }
}
