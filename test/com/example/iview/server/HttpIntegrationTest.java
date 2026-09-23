package com.example.iview.server;

import com.example.iview.TestKit;
import com.example.iview.json.Json;
import com.example.iview.view.MaterializedView;
import com.sun.net.httpserver.HttpServer;

import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

/**
 * End-to-end HTTP tests: boots the real {@link ViewHandler} on an ephemeral
 * port and drives it with the JDK {@link HttpClient}. Covers the acceptance
 * scenarios over the wire, including dedupe, category change, missing-row
 * deletes, batch ordering, and repeated verify comparisons.
 */
public final class HttpIntegrationTest {

    private HttpClient client;
    private HttpServer server;
    private String base;
    private int seq;

    public static int run() throws Exception {
        HttpIntegrationTest h = new HttpIntegrationTest();
        h.setUp();
        int exit;
        try {
            exit = h.runTests();
        } finally {
            h.tearDown();
        }
        return exit;
    }

    private void setUp() throws Exception {
        AtomicReference<MaterializedView> state =
                new AtomicReference<>(new MaterializedView());
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", new ViewHandler(state));
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        base = "http://127.0.0.1:" + server.getAddress().getPort();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private void tearDown() {
        server.stop(0);
    }

    private int runTests() throws Exception {
        TestKit t = new TestKit("HttpIntegrationTest");
        testEmptyView(t);
        testAcceptanceScenarioOverHttp(t);
        testBatch(t);
        testDuplicateAcrossBatches(t);
        testDeleteMissingOverHttp(t);
        testValidationErrors(t);
        testReset(t);
        testFixedPointOverHttp(t);
        return t.finish();
    }

    private void testEmptyView(TestKit t) throws Exception {
        t.section("GET /view on empty state");
        Map<String, Object> view = get("/view");
        t.checkEq(List.of(), view.get("aggregates"), "no aggregates");
        t.checkEq(Boolean.TRUE, view.get("matchesFullRecompute"), "empty verifies");
    }

    private void testAcceptanceScenarioOverHttp(TestKit t) throws Exception {
        t.section("acceptance: insert, recategorize, dedupe, delete, compare each step");

        postEvent("""
                {"eventId":"%s","side":"product","op":"upsert",
                 "product":{"productId":"P1","category":"books"}}""");
        postEvent("""
                {"eventId":"%s","side":"product","op":"upsert",
                 "product":{"productId":"P2","category":"books"}}""");
        postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"L1","productId":"P1","qty":2,"amount":"10.00"}}""");
        postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"L2","productId":"P2","qty":1,"amount":"5.00"}}""");

        Map<String, Object> view = get("/view");
        t.checkEq(2, ((Number) view.get("orderLineCount")).intValue(), "two order lines stored");
        t.checkEq(Boolean.TRUE, view.get("matchesFullRecompute"), "initial verify");

        // Duplicate business event replayed exactly -> must not double count
        String dup = "http-dup";
        Map<String, Object> first = postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"L3","productId":"P1","qty":4,"amount":"40.00"}}""",
                dup);
        t.checkEq(Boolean.FALSE, nested(first, "result", "duplicate"), "first apply not duplicate");
        Map<String, Object> replay = postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"L3","productId":"P1","qty":4,"amount":"40.00"}}""",
                dup);
        t.checkEq(Boolean.TRUE, nested(replay, "result", "duplicate"), "replay is duplicate");

        Map<String, Object> verify = get("/verify");
        t.checkEq(Boolean.TRUE, verify.get("matches"), "verify after duplicate");
        Map<String, Object> view2 = get("/view");
        t.checkEq(money("55.00"), amountOf(view2, "books"), "books amount still 55.00");
        t.checkEq(7L, qtyOf(view2, "books"), "books qty still 7");

        // Dimension category change P1 books -> media: L1 and L3 both move
        Map<String, Object> recat = postEvent("""
                {"eventId":"%s","side":"product","op":"upsert",
                 "product":{"productId":"P1","category":"media"}}""");
        t.checkEq(2, ((Number) nested(recat, "result", "migratedOrderLines")).intValue(),
                "both P1 lines (L1,L3) migrated");
        t.checkEq(Boolean.TRUE, recat.get("verified"), "verified after recategorization");

        Map<String, Object> after = get("/view");
        t.checkEq(money("50.00"), amountOf(after, "media"),
                "media holds L1 10.00 + L3 40.00 = 50.00");
        t.checkEq(money("5.00"), amountOf(after, "books"), "books keeps only L2 5.00");

        // Full side-by-side verification
        Map<String, Object> verify2 = get("/verify");
        t.checkEq(Boolean.TRUE, verify2.get("matches"), "incremental == full recompute");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> comparison = (List<Map<String, Object>>) verify2.get("comparison");
        comparison.forEach(row -> t.checkEq(Boolean.TRUE, row.get("equal"),
                "row equal: " + row.get("category")));

        // Delete a line and verify once more
        postEvent("""
                {"eventId":"%s","side":"order_line","op":"delete",
                 "orderLine":{"orderLineId":"L2"}}""");
        Map<String, Object> finalView = get("/view");
        t.check(finalView.get("aggregates") != null, "view still served");
        t.checkEq(Boolean.TRUE, finalView.get("matchesFullRecompute"), "verify after delete");
        t.check(amountOf(finalView, "books") == null, "books bucket pruned (L2 removed)");
        t.checkEq(money("50.00"), amountOf(finalView, "media"), "media still 50.00");
    }

    private void testBatch(TestKit t) throws Exception {
        t.section("POST /events/batch applies in order; one duplicate inside");
        String body = """
                {"events":[
                  {"eventId":"b-1","side":"product","op":"upsert",
                   "product":{"productId":"B1","category":"food"}},
                  {"eventId":"b-2","side":"order_line","op":"upsert",
                   "orderLine":{"orderLineId":"BL1","productId":"B1","qty":1,"amount":"3.30"}},
                  {"eventId":"b-2","side":"order_line","op":"upsert",
                   "orderLine":{"orderLineId":"BL1","productId":"B1","qty":1,"amount":"3.30"}},
                  {"eventId":"b-3","side":"order_line","op":"upsert",
                   "orderLine":{"orderLineId":"BL2","productId":"B1","qty":2,"amount":"6.70"}}
                ]}""";
        Map<String, Object> resp = post("/events/batch", body);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) resp.get("results");
        t.checkEq(4, results.size(), "four result entries");
        t.checkEq(Boolean.FALSE, results.get(0).get("duplicate"), "b-1 applied");
        t.checkEq(Boolean.TRUE, results.get(2).get("duplicate"), "third entry (b-2) duplicate");
        t.checkEq(Boolean.TRUE, resp.get("verified"), "batch verified");

        Map<String, Object> view = get("/view");
        t.checkEq(money("10.00"), amountOf(view, "food"), "food = 3.30 + 6.70 = 10.00 exactly");
        t.checkEq(3L, qtyOf(view, "food"), "food qty 3");
    }

    private void testDuplicateAcrossBatches(TestKit t) throws Exception {
        t.section("an eventId used in one request is remembered in the next");
        String event = """
                {"eventId":"cross-1","side":"product","op":"upsert",
                 "product":{"productId":"X1","category":"cat-x"}}""";
        post("/events", event);
        Map<String, Object> again = post("/events", event);
        t.checkEq(Boolean.TRUE, nested(again, "result", "duplicate"), "deduped across requests");
    }

    private void testDeleteMissingOverHttp(TestKit t) throws Exception {
        t.section("deleting missing rows over HTTP returns changed:false, notFound:true");
        Map<String, Object> r1 = postEvent("""
                {"eventId":"%s","side":"order_line","op":"delete",
                 "orderLine":{"orderLineId":"GHOST-LINE"}}""");
        t.checkEq(Boolean.TRUE, nested(r1, "result", "notFound"), "line not found");
        t.checkEq(Boolean.FALSE, nested(r1, "result", "changed"), "nothing changed");

        Map<String, Object> r2 = postEvent("""
                {"eventId":"%s","side":"product","op":"delete",
                 "product":{"productId":"GHOST-PROD"}}""");
        t.checkEq(Boolean.TRUE, nested(r2, "result", "notFound"), "product not found");
    }

    private void testValidationErrors(TestKit t) throws Exception {
        t.section("malformed requests return 400 and do not corrupt state");
        int status1 = postStatus("/events", "{not json");
        t.checkEq(400, status1, "invalid json -> 400");
        int status2 = postStatus("/events",
                "{\"side\":\"product\",\"op\":\"upsert\"}");
        t.checkEq(400, status2, "missing eventId -> 400");
        int status3 = postStatus("/events/batch", "{}");
        t.checkEq(400, status3, "missing events array -> 400");
        int status4 = postStatus("/events", "{}");
        t.checkEq(400, status4, "empty body object -> 400");

        Map<String, Object> verify = get("/verify");
        t.checkEq(Boolean.TRUE, verify.get("matches"), "state still consistent after 400s");
    }

    private void testReset(TestKit t) throws Exception {
        t.section("POST /reset clears tables and dedupe history");
        String event = """
                {"eventId":"reuse-r1","side":"product","op":"upsert",
                 "product":{"productId":"R1","category":"tmp"}}""";
        post("/events", event);
        post("/reset", "");
        Map<String, Object> view = get("/view");
        t.checkEq(0, ((Number) view.get("productCount")).intValue(), "products cleared");
        t.checkEq(0, ((Number) view.get("distinctSeenEventIds")).intValue(),
                "seen event ids cleared");

        // The same eventId is accepted again after reset: proves the dedupe
        // scope does not survive a reset.
        Map<String, Object> reused = post("/events", event);
        t.checkEq(Boolean.FALSE, nested(reused, "result", "duplicate"),
                "id accepted again after reset");
    }

    private void testFixedPointOverHttp(TestKit t) throws Exception {
        t.section("fixed-point money survives the JSON/HTTP layer");
        post("/reset", "");
        postEvent("""
                {"eventId":"%s","side":"product","op":"upsert",
                 "product":{"productId":"M1","category":"money"}}""");
        postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"M1","productId":"M1","qty":1,"amount":"0.10"}}""");
        postEvent("""
                {"eventId":"%s","side":"order_line","op":"upsert",
                 "orderLine":{"orderLineId":"M2","productId":"M1","qty":1,"amount":"0.20"}}""");
        Map<String, Object> view = get("/view");
        t.checkEq("0.30", amountString(view, "money"), "0.10 + 0.20 = exactly 0.30 over HTTP");
    }

    // ------------------------------------------------------------- helpers

    private String nextId() {
        return "http-" + (seq++);
    }

    private Map<String, Object> postEvent(String template) throws Exception {
        return postEvent(template, nextId());
    }

    private Map<String, Object> postEvent(String template, String eventId) throws Exception {
        String body = template.formatted(eventId);
        return post("/events", body);
    }

    private Map<String, Object> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 200) {
            throw new AssertionError("GET " + path + " -> " + resp.statusCode() + ": " + resp.body());
        }
        return Json.parseObject(resp.body());
    }

    private Map<String, Object> post(String path, String body) throws Exception {
        int status = postStatus(path, body);
        if (status != 200) {
            throw new AssertionError("POST " + path + " -> " + status + " for body: " + body);
        }
        // re-read not possible; parse from a fresh send inside postStatus variant
        return Json.parseObject(lastBody);
    }

    private String lastBody;

    private int postStatus(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        lastBody = resp.body();
        return resp.statusCode();
    }

    @SuppressWarnings("unchecked")
    private static Object nested(Map<String, Object> m, String parent, String key) {
        return ((Map<String, Object>) m.get(parent)).get(key);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> aggRow(Map<String, Object> view, String category) {
        for (Object o : (List<Object>) view.get("aggregates")) {
            Map<String, Object> row = (Map<String, Object>) o;
            if (category.equals(row.get("category"))) {
                return row;
            }
        }
        return null;
    }

    private static BigDecimal money(String s) {
        return new BigDecimal(s).setScale(2);
    }

    private static Long qtyOf(Map<String, Object> view, String category) {
        Map<String, Object> row = aggRow(view, category);
        return row == null ? null : ((Number) row.get("totalQty")).longValue();
    }

    private static BigDecimal amountOf(Map<String, Object> view, String category) {
        String s = amountString(view, category);
        return s == null ? null : new BigDecimal(s);
    }

    private static String amountString(Map<String, Object> view, String category) {
        Map<String, Object> row = aggRow(view, category);
        return row == null ? null : (String) row.get("totalAmount");
    }
}
