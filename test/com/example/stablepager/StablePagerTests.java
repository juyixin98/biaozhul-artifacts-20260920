package com.example.stablepager;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Zero-dependency end-to-end test runner. Drives the real HTTP server over a
 * loopback socket with the JDK HttpClient and asserts the acceptance scenarios:
 * <ol>
 *   <li>full walks are stable with duplicate sort keys,</li>
 *   <li>inserts/deletes/score changes during a walk neither repeat nor drop rows,</li>
 *   <li>changing filters/sort/order/limit invalidates the cursor,</li>
 *   <li>forged / foreign-secret / tampered cursors are rejected,</li>
 *   <li>expired snapshots fail with an explicit error,</li>
 *   <li>descending order, filtering and CRUD/validation behave.</li>
 * </ol>
 * Exit code is non-zero if anything fails.
 */
public final class StablePagerTests {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    private static final String SECRET = "unit-test-secret-do-not-use-in-prod";
    private static final long TTL_MS = 800; // short so expiry tests run quickly

    private static WebRuntime app;
    private static String base;
    private static final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    public static void main(String[] args) throws Exception {
        app = WebRuntime.start(0, TTL_MS, SECRET);
        base = "http://localhost:" + app.port();
        SeedData.seed(app.store());
        try {
            testHealth();
            testFullWalkStableWithDuplicateScores();
            testDescendingWalk();
            testSnapshotIsolationDuringMutations();
            testFilteredWalkIsStableAndComplete();
            testChangingQueryRejectsCursor();
            testForgedCursors();
            testExpiredSnapshot();
            testCrudAndValidation();
            testCursorCodecUnit();
        } finally {
            app.close();
        }

        System.out.println();
        System.out.println("--------------------------------------------------");
        System.out.println("passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            for (String f : failures) {
                System.out.println("FAIL: " + f);
            }
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------
    // Tests
    // ------------------------------------------------------------------

    static void testHealth() {
        check("healthz 200", get("/healthz").status() == 200);
    }

    static void testFullWalkStableWithDuplicateScores() {
        List<String> baseline = walkAll("", null);

        // 23 distinct rows, no repeats
        check("full walk returns 23 rows", baseline.size() == 23);
        check("full walk has distinct ids", baseline.stream().distinct().count() == 23);

        // expected ascending score, ties broken by id ascending
        List<String> expected = new ArrayList<>(baseline);
        expected.sort((x, y) -> {
            Map<String, Object> a = INDEX.get(x);
            Map<String, Object> b = INDEX.get(y);
            int c = Long.compare(((Number) a.get("score")).longValue(), ((Number) b.get("score")).longValue());
            return c != 0 ? c : x.compareTo(y);
        });
        check("order is (score asc, id asc) incl. duplicate scores", baseline.equals(expected));

        // score 50 repeats five times: a-005..a-009 in id order
        int i50 = baseline.indexOf("a-005");
        check("duplicate-score block ordered by id a-005..a-009",
                baseline.get(i50).equals("a-005")
                        && baseline.get(i50 + 1).equals("a-006")
                        && baseline.get(i50 + 2).equals("a-007")
                        && baseline.get(i50 + 3).equals("a-008")
                        && baseline.get(i50 + 4).equals("a-009"));

        // different page sizes must yield the identical walk
        for (int size : new int[]{1, 2, 7, 23, 100}) {
            List<String> again = walkAll("limit=" + size, null);
            check("walk identical with limit=" + size, again.equals(baseline));
        }
    }

    static void testDescendingWalk() {
        List<String> desc = walkAll("sort=score&order=desc&limit=3", null);
        List<String> expected = walkAll("limit=100", null);
        expected.sort((x, y) -> {
            long sx = ((Number) INDEX.get(x).get("score")).longValue();
            long sy = ((Number) INDEX.get(y).get("score")).longValue();
            int c = Long.compare(sy, sx); // score descending
            return c != 0 ? c : x.compareTo(y); // id tie-break still ascending
        });
        check("descending walk matches (score desc, id asc)", desc.equals(expected));

        // duplicate-score block under desc: highest scores first, but the five
        // 50s still come out a-005..a-009
        int i50 = desc.indexOf("a-005");
        check("desc tie-break still id asc", desc.get(i50).equals("a-005")
                && desc.get(i50 + 1).equals("a-006")
                && desc.get(i50 + 4).equals("a-009"));
    }

    static void testSnapshotIsolationDuringMutations() {
        // Baseline and page 1 are pinned to the same pre-mutation version.
        List<String> baseline = walkAll("limit=100", null); // snapshot S0, version V

        Response first = get("/api/items?limit=10"); // snapshot S1, version V
        List<String> page1 = ids(first);
        check("first page has 10 rows", page1.size() == 10);
        String cursor = cursor(first);
        String snapshotId = (String) pageMeta(first).get("snapshotId");

        // Mutate WHILE the walk is in progress (page 1 returned positions 1-10).
        post("/api/items", map("id", "new-low", "name", "ZZ New Low", "category", "books", "score", 15L));
        post("/api/items", map("id", "new-mid", "name", "ZZ New Mid", "category", "music", "score", 50L));
        post("/api/items", map("id", "new-high", "name", "ZZ New High", "category", "games", "score", 999L));
        delete("/api/items/a-010"); // not yet seen (score 60, position 12)
        delete("/api/items/a-020"); // not yet seen (score 150)
        patch("/api/items/a-009", map("score", 51L)); // duplicate-score row, position 11: not yet seen
        patch("/api/items/a-001", map("score", 11L));  // already seen on page 1

        List<String> rest = walkAll("limit=10", cursor);
        List<String> walked = new ArrayList<>(page1);
        walked.addAll(rest);

        check("mutation during walk: same 23 rows, no insert leaks in", walked.equals(baseline));
        check("mutation during walk: no repeats", walked.stream().distinct().count() == walked.size());

        // the continued responses stay pinned to the ORIGINAL snapshot
        Response second = get("/api/items?limit=10&cursor=" + enc(cursor));
        check("continuation keeps the same snapshotId",
                snapshotId.equals(pageMeta(second).get("snapshotId")));
        Map<String, Object> a009 = findOne(walkedItems(first, second), "a-009");
        check("old score value visible inside snapshot (a-009 still 50, live is 51)",
                a009 != null && ((Number) a009.get("score")).longValue() == 50L);

        // ...but a brand-new first page reflects the new live version
        Response fresh = get("/api/items?limit=100");
        List<String> freshIds = ids(fresh);
        check("new walk sees inserted rows", freshIds.contains("new-low") && freshIds.contains("new-high"));
        check("new walk does not see deleted rows", !freshIds.contains("a-010") && !freshIds.contains("a-020"));
        long v0 = ((Number) pageMeta(first).get("snapshotVersion")).longValue();
        long vNew = ((Number) pageMeta(fresh).get("snapshotVersion")).longValue();
        check("new walk is on a newer version", vNew > v0);

        // restore seed state for later tests
        delete("/api/items/new-low");
        delete("/api/items/new-mid");
        delete("/api/items/new-high");
        post("/api/items", map("id", "a-010", "name", "Juliet", "category", "games", "score", 60L));
        post("/api/items", map("id", "a-020", "name", "Tango", "category", "music", "score", 150L));
        patch("/api/items/a-009", map("score", 50L));
        patch("/api/items/a-001", map("score", 10L));
        check("seed restored to 23 rows", ids(get("/api/items?limit=100")).size() == 23);
    }

    static void testFilteredWalkIsStableAndComplete() {
        List<String> books = walkAll("category=books&limit=3", null);
        check("category filter: 8 books", books.size() == 8);
        check("all ids are books", books.stream().allMatch(id -> "books".equals(INDEX.get(id).get("category"))));

        // nameContains is a case-insensitive substring match
        List<String> alpha = walkAll("nameContains=" + enc("alp") + "&limit=2", null);
        check("nameContains=alp matches Alpha only", alpha.size() == 1 && alpha.contains("a-001"));

        // filter + mid-walk insert still stable
        Response first = get("/api/items?category=music&limit=2");
        String cursor = cursor(first);
        post("/api/items", map("id", "music-intruder", "name", "Intruder", "category", "music", "score", 50L));
        List<String> rest = walkAll("category=music&limit=2", cursor);
        List<String> walked = new ArrayList<>(ids(first));
        walked.addAll(rest);
        check("filtered walk ignores mid-walk insert", !walked.contains("music-intruder") && walked.size() == 8);
        delete("/api/items/music-intruder");
    }

    static void testChangingQueryRejectsCursor() {
        Response first = get("/api/items?category=music&sort=score&order=asc&limit=5");
        String cursor = cursor(first);

        expect400("change category", "/api/items?category=books&limit=5&cursor=" + enc(cursor), "QUERY_MISMATCH");
        expect400("add nameContains",
                "/api/items?category=music&limit=5&nameContains=" + enc("a") + "&cursor=" + enc(cursor),
                "QUERY_MISMATCH");
        expect400("change sort field", "/api/items?sort=name&limit=5&cursor=" + enc(cursor), "QUERY_MISMATCH");
        expect400("change order", "/api/items?category=music&limit=5&order=desc&cursor=" + enc(cursor),
                "QUERY_MISMATCH");
        expect400("change limit", "/api/items?category=music&limit=7&cursor=" + enc(cursor), "QUERY_MISMATCH");

        // a cursor from the unfiltered query cannot drive a filtered one
        Response unfiltered = get("/api/items?limit=5");
        expect400("reuse unfiltered cursor with a filter",
                "/api/items?category=music&limit=5&cursor=" + enc(cursor(unfiltered)), "QUERY_MISMATCH");
    }

    static void testForgedCursors() {
        expect400("garbage token", "/api/items?limit=5&cursor=not-a-real-cursor", "CURSOR_INVALID");
        expect400("wrong version prefix", "/api/items?limit=5&cursor=v2.aaa.bbb", "CURSOR_INVALID");
        expect400("truncated token", "/api/items?limit=5&cursor=v1.aaa", "CURSOR_INVALID");
        expect400("non-base64 token", "/api/items?limit=5&cursor=v1.@@@.@@@", "CURSOR_INVALID");

        Response first = get("/api/items?limit=5");
        String token = cursor(first);

        // flip a payload character -> HMAC must fail
        char[] chars = token.toCharArray();
        int flip = token.indexOf('.') + 3;
        chars[flip] = chars[flip] == 'A' ? 'B' : 'A';
        expect400("payload byte tampered", "/api/items?limit=5&cursor=" + enc(new String(chars)),
                "CURSOR_INVALID");

        // flip a tag character
        char[] tagFlip = token.toCharArray();
        tagFlip[tagFlip.length - 2] = tagFlip[tagFlip.length - 2] == 'A' ? 'B' : 'A';
        expect400("tag byte tampered", "/api/items?limit=5&cursor=" + enc(new String(tagFlip)),
                "CURSOR_INVALID");

        // token signed by a different secret (e.g. another server / restart w/ new key)
        CursorCodec foreign = new CursorCodec("a-totally-different-secret");
        Map<String, Object> forgedPayload = new LinkedHashMap<>();
        forgedPayload.put("f", "name=|category=|sort=score|order=asc|limit=5");
        forgedPayload.put("s", "deadbeef");
        forgedPayload.put("v", 1L);
        forgedPayload.put("k", 50L);
        forgedPayload.put("id", "a-005");
        expect400("foreign-secret signature",
                "/api/items?limit=5&cursor=" + enc(foreign.encode(forgedPayload)), "CURSOR_INVALID");

        // properly signed but pointing at a snapshot that never existed
        CursorCodec insider = new CursorCodec(SECRET);
        Map<String, Object> unknownSnapshot = new LinkedHashMap<>(forgedPayload);
        expect410("valid signature, unknown snapshot",
                "/api/items?limit=5&cursor=" + enc(insider.encode(unknownSnapshot)));

        // properly signed but snapshot/version disagree
        Response other = get("/api/items?limit=5");
        String goodSnapshot = (String) pageMeta(other).get("snapshotId");
        Map<String, Object> badVersion = new LinkedHashMap<>(forgedPayload);
        badVersion.put("s", goodSnapshot);
        badVersion.put("v", 999_999L);
        expect400("signed cursor with wrong version",
                "/api/items?limit=5&cursor=" + enc(insider.encode(badVersion)), "CURSOR_INVALID");
    }

    static void testExpiredSnapshot() {
        Response first = get("/api/items?limit=5");
        String cursor = cursor(first);
        sleep(TTL_MS + 300); // past TTL; janitor (period ttl/2) has also swept it
        Response res = get("/api/items?limit=5&cursor=" + enc(cursor));
        check("expired snapshot -> 410", res.status() == 410);
        check("expired snapshot error code", "SNAPSHOT_EXPIRED".equals(errorCode(res)));

        // within TTL it still works
        Response a = get("/api/items?limit=5");
        sleep(TTL_MS / 3);
        Response b = get("/api/items?limit=5&cursor=" + enc(cursor(a)));
        check("cursor within TTL works", b.status() == 200 && ids(b).size() == 5);
    }

    static void testCrudAndValidation() {
        Response created = post("/api/items",
                map("id", "t-1", "name", "Test Row", "category", "books", "score", 42L));
        check("create 201", created.status() == 201
                && "t-1".equals(created.body().get("id")));

        Response conflict = post("/api/items",
                map("id", "t-1", "name", "Dup", "category", "books", "score", 1L));
        check("duplicate id -> 409", conflict.status() == 409 && "ID_CONFLICT".equals(errorCode(conflict)));

        Response patched = patch("/api/items/t-1", map("name", "Renamed", "score", 43L));
        check("patch 200", patched.status() == 200
                && "Renamed".equals(((Map<?, ?>) patched.body().get("item")).get("name")));
        // re-check via GET
        Response fetched = get("/api/items/t-1");
        check("patched name persisted", "Renamed".equals(((Map<?, ?>) fetched.body().get("item")).get("name")));
        check("patched score persisted",
                ((Number) ((Map<?, ?>) fetched.body().get("item")).get("score")).longValue() == 43L);

        check("get missing -> 404", get("/api/items/does-not-exist").status() == 404);
        check("delete 200", delete("/api/items/t-1").status() == 200);
        check("delete again -> 404", delete("/api/items/t-1").status() == 404);

        check("missing required field -> 400 MISSING_FIELD",
                "MISSING_FIELD".equals(errorCode(post("/api/items", map("id", "t-2", "name", "NoCategory")))));
        check("wrong score type -> 400 INVALID_FIELD",
                "INVALID_FIELD".equals(errorCode(post("/api/items",
                        map("id", "t-3", "name", "X", "category", "books", "score", "fifty")))));
        check("bad JSON -> 400 BAD_JSON",
                "BAD_JSON".equals(errorCode(postRaw("/api/items", "{not json"))));
        check("invalid limit -> 400", "INVALID_LIMIT".equals(errorCode(get("/api/items?limit=0"))));
        check("limit > 100 -> 400", "INVALID_LIMIT".equals(errorCode(get("/api/items?limit=101"))));
        check("invalid sort -> 400", "INVALID_SORT".equals(errorCode(get("/api/items?sort=nope"))));
        check("invalid order -> 400", "INVALID_ORDER".equals(errorCode(get("/api/items?order=sideways"))));
        check("unknown path -> 404", get("/api/nope").status() == 404);
        check("method not allowed -> 405", put("/api/items", "{}").status() == 405);
    }

    static void testCursorCodecUnit() {
        CursorCodec codec = new CursorCodec(SECRET);
        CursorCodec other = new CursorCodec("other");
        Map<String, Object> p = new LinkedHashMap<>();
        p.put("f", "fp");
        p.put("s", "snap");
        p.put("v", 7L);
        p.put("k", 50L);
        p.put("id", "a-005");
        String token = codec.encode(p);
        check("codec roundtrip", codec.decode(token).get("id").equals("a-005"));
        check("foreign secret rejected",
                expectThrows(() -> codec.decode(other.encode(p))) instanceof ApiException ae
                        && "CURSOR_INVALID".equals(ae.code()));
        check("garbage rejected", expectThrows(() -> codec.decode("v1.aa.bb")) instanceof ApiException);
    }

    // ------------------------------------------------------------------
    // HTTP helpers
    // ------------------------------------------------------------------

    /** id -> item json, populated while walking responses (score/category lookup). */
    private static final Map<String, Map<String, Object>> INDEX = new LinkedHashMap<>();

    @SuppressWarnings("unchecked")
    private static Map<String, Object> findOne(List<Map<String, Object>> items, String id) {
        return items.stream().filter(i -> id.equals(i.get("id"))).findFirst().orElse(null);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> walkedItems(Response first, Response second) {
        List<Map<String, Object>> out = new ArrayList<>(items(first));
        out.addAll(items(second));
        return out;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> items(Response r) {
        return (List<Map<String, Object>>) r.body().get("items");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> pageMeta(Response r) {
        return (Map<String, Object>) r.body().get("page");
    }

    private static List<String> ids(Response r) {
        List<String> ids = new ArrayList<>();
        for (Map<String, Object> item : items(r)) {
            ids.add((String) item.get("id"));
            INDEX.put((String) item.get("id"), item);
        }
        return ids;
    }

    private static String cursor(Response r) {
        return (String) r.body().get("nextCursor");
    }

    /** Walks every page from the start (or a given cursor) and returns all ids in order. */
    private static List<String> walkAll(String query, String startCursor) {
        List<String> out = new ArrayList<>();
        String cursor = startCursor;
        int guard = 0;
        while (true) {
            String url = "/api/items" + (query.isEmpty() ? "" : "?" + query);
            if (cursor != null) {
                url += (url.contains("?") ? "&" : "?") + "cursor=" + enc(cursor);
            }
            Response r = get(url);
            if (r.status() != 200) {
                throw new AssertionError("walk failed: " + r.status() + " " + r.text());
            }
            out.addAll(ids(r));
            cursor = cursor(r);
            if (cursor == null) {
                return out;
            }
            if (++guard > 500) {
                throw new AssertionError("walk did not terminate");
            }
        }
    }

    private static void expect400(String name, String path, String expectedCode) {
        Response r = get(path);
        check(name + " -> 400 " + expectedCode,
                r.status() == 400 && expectedCode.equals(errorCode(r)));
    }

    private static void expect410(String name, String path) {
        Response r = get(path);
        check(name + " -> 410 SNAPSHOT_EXPIRED",
                r.status() == 410 && "SNAPSHOT_EXPIRED".equals(errorCode(r)));
    }

    private static String errorCode(Response r) {
        Object code = r.body().get("error");
        return code == null ? null : code.toString();
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    record Response(int status, Map<String, Object> body, String text) {
    }

    private static Response request(String method, String path, String jsonBody) {
        try {
            HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
            if (jsonBody != null) {
                b.header("Content-Type", "application/json");
                b.method(method, HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8));
            } else {
                b.method(method, HttpRequest.BodyPublishers.noBody());
            }
            HttpResponse<String> res = http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            Object parsed = res.body().isBlank() ? Map.of() : Json.parse(res.body());
            @SuppressWarnings("unchecked")
            Map<String, Object> map = parsed instanceof Map<?, ?> ? (Map<String, Object>) parsed : Map.of();
            return new Response(res.statusCode(), map, res.body());
        } catch (Exception e) {
            throw new RuntimeException("HTTP " + method + " " + path + " failed: " + e, e);
        }
    }

    private static Response get(String path) {
        return request("GET", path, null);
    }

    private static Response postRaw(String path, String body) {
        return request("POST", path, body);
    }

    private static Response post(String path, Map<String, Object> body) {
        return request("POST", path, Json.stringify(body));
    }

    private static Response patch(String path, Map<String, Object> body) {
        return request("PATCH", path, Json.stringify(body));
    }

    private static Response delete(String path) {
        return request("DELETE", path, null);
    }

    private static Response put(String path, String body) {
        return request("PUT", path, body);
    }

    private static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  ok   " + name);
        } else {
            failed++;
            failures.add(name);
            System.out.println("  FAIL " + name);
        }
    }

    private static Throwable expectThrows(Runnable r) {
        try {
            r.run();
            return null;
        } catch (Throwable t) {
            return t;
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
