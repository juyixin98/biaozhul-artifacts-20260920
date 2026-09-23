package vecsearch.server;

import vecsearch.core.SearchHit;
import vecsearch.core.SearchOutcome;
import vecsearch.core.TaggedVector;
import vecsearch.core.VectorStore;
import vecsearch.index.IndexOptions;
import vecsearch.json.Json;
import vecsearch.json.JsonWriter;
import vecsearch.util.ApiException;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * HTTP 路由处理器。所有接口收发 JSON。
 *
 * <pre>
 * POST   /v1/vectors          插入/替换（单条或 {'vectors':[...]} 批量）
 * GET    /v1/vectors/{id}     查看一条
 * DELETE /v1/vectors/{id}     删除一条
 * POST   /v1/search/exact     精确基线检索
 * POST   /v1/search           近似（IVF）检索，body 带 nprobe 预算
 * POST   /v1/index/rebuild    用指定 nlist/iters/seed 重新训练
 * GET    /v1/stats            集合与索引信息
 * GET    /healthz             存活探针
 * GET    /                    接口清单
 * </pre>
 */
public final class ApiHandler implements HttpHandler {

    private final VectorStore store;

    public ApiHandler(VectorStore store) {
        this.store = store;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        try {
            route(ex);
        } catch (ApiException ae) {
            respond(ex, ae.status(), Map.of("error", ae.getMessage()));
        } catch (Exception e) {
            respond(ex, 500, Map.of("error", "internal error: " + e));
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        String method = ex.getRequestMethod();
        String path = ex.getRequestURI().getPath();

        if ("GET".equals(method) && "/healthz".equals(path)) {
            respond(ex, 200, Map.of("status", "ok", "metric", store.metric().name()));
            return;
        }
        if ("GET".equals(method) && ("/".equals(path) || "/v1".equals(path))) {
            respond(ex, 200, apiMap());
            return;
        }
        if ("GET".equals(method) && "/v1/stats".equals(path)) {
            respond(ex, 200, store.stats());
            return;
        }
        if ("POST".equals(method) && "/v1/vectors".equals(path)) {
            handleUpsert(ex);
            return;
        }
        if (path.startsWith("/v1/vectors/")) {
            String id = decode(path.substring("/v1/vectors/".length()));
            if (id.isEmpty() || id.contains("/")) {
                throw ApiException.badRequest("invalid vector id in path");
            }
            if ("GET".equals(method)) {
                handleGet(ex, id);
            } else if ("DELETE".equals(method)) {
                handleDelete(ex, id);
            } else {
                throw new ApiException(405, "method not allowed on " + path);
            }
            return;
        }
        if ("POST".equals(method) && "/v1/search/exact".equals(path)) {
            handleSearch(ex, true);
            return;
        }
        if ("POST".equals(method) && ("/v1/search".equals(path) || "/v1/search/approx".equals(path))) {
            handleSearch(ex, false);
            return;
        }
        if ("POST".equals(method) && "/v1/index/rebuild".equals(path)) {
            handleRebuild(ex);
            return;
        }
        throw new ApiException(404, "no route for " + method + " " + path);
    }

    // ------------------------------------------------------------------
    // 具体接口
    // ------------------------------------------------------------------

    private void handleUpsert(HttpExchange ex) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        List<TaggedVector> batch = Requests.parseUpsertBody(body);
        int inserted = store.upsertAll(batch);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("upserted", batch.size());
        resp.put("created", inserted);
        resp.put("updated", batch.size() - inserted);
        resp.put("count", store.size());
        respond(ex, 200, resp);
    }

    private void handleGet(HttpExchange ex, String id) throws IOException {
        TaggedVector v = store.get(id);
        if (v == null) {
            throw ApiException.notFound("vector not found: " + id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("id", v.id());
        resp.put("vector", toNumberList(v.vector()));
        resp.put("filter", v.filter());
        respond(ex, 200, resp);
    }

    private void handleDelete(HttpExchange ex, String id) throws IOException {
        boolean existed = store.delete(id);
        if (!existed) {
            throw ApiException.notFound("vector not found: " + id);
        }
        respond(ex, 200, Map.of("deleted", id, "count", store.size()));
    }

    private void handleSearch(HttpExchange ex, boolean exact) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        float[] query = Requests.requireVector(body, "vector");
        int k = Requests.optionalInt(body, "k", 10);
        Map<String, String> filter = Requests.optionalFilter(body, "filter");

        SearchOutcome outcome;
        Integer nprobe = null;
        if (exact) {
            outcome = store.exactSearch(query, k, filter);
        } else {
            nprobe = Requests.optionalInt(body, "nprobe", 0);
            outcome = store.approxSearch(query, k, filter, nprobe);
        }
        respond(ex, 200, searchResponse(outcome, nprobe));
    }

    private void handleRebuild(HttpExchange ex) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        int nlist = Requests.optionalInt(body, "nlist", 0);
        int iters = Requests.optionalInt(body, "maxIters", IndexOptions.DEFAULT_MAX_ITERS);
        long seed = Requests.optionalLong(body, "seed", IndexOptions.DEFAULT_SEED);
        Map<String, Object> desc = store.rebuildIndex(new IndexOptions(nlist, iters, seed));
        respond(ex, 200, Map.of("rebuilt", true, "index", desc));
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------

    static Map<String, Object> searchResponse(SearchOutcome o, Integer nprobe) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("exact", o.exact());
        if (!o.exact()) {
            resp.put("nprobe", nprobe);
        }
        resp.put("distanceComputations", o.computations());
        resp.put("returned", o.hits().size());
        List<Map<String, Object>> hits = new ArrayList<>(o.hits().size());
        for (SearchHit h : o.hits()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", h.id());
            m.put("distance", h.distance());
            m.put("filter", h.filter());
            hits.add(m);
        }
        resp.put("hits", hits);
        return resp;
    }

    private static List<Double> toNumberList(float[] v) {
        List<Double> l = new ArrayList<>(v.length);
        for (float f : v) {
            l.add((double) f);
        }
        return l;
    }

    private Map<String, Object> apiMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("service", "vector-nearest-neighbor");
        m.put("metric", store.metric().name());
        m.put("endpoints", List.of(
                "POST   /v1/vectors",
                "GET    /v1/vectors/{id}",
                "DELETE /v1/vectors/{id}",
                "POST   /v1/search/exact",
                "POST   /v1/search  (body: nprobe budget)",
                "POST   /v1/index/rebuild",
                "GET    /v1/stats",
                "GET    /healthz"));
        return m;
    }

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static void respond(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = JsonWriter.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }
}
