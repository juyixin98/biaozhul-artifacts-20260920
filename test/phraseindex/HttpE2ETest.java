package phraseindex;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** End-to-end tests of the HTTP API against a real server on an ephemeral port. */
public final class HttpE2ETest {

    private static final String BASE = "http://localhost:";

    private static HttpServerApp app;
    private static String root;
    private static HttpClient client;

    public static void register(Suite s) {
        s.test("HTTP: full API lifecycle, phrases, booleans, stale-free replacement", () -> {
            startServer();

            assertStatus(200, request("GET", "/health", null));
            Map<?, ?> health = body(request("GET", "/health", null));
            Suite.assertEquals(0, ((Number) health.get("documents")).intValue(), "empty start");
            // Index docs 1..4 with PUT (raw text). Includes repeated words and
            // runs of spaces.
            put(1, "the quick brown fox");
            put(2, "go go go");
            put(3, "go");                 // cross-document boundary partner
            put(4, "  hello   world  ");  // double spaces

            // Exact phrase: repeated-word phrase in "go go go" starts at 0.
            Map<?, ?> goPhrase = search("\"go go go\"", true);
            assertDocIds(List.of(2L), goPhrase, "go-go-go doc");
            Map<?, ?> positions = (Map<?, ?>) goPhrase.get("positions");
            Suite.assertEquals(List.of(0), intList(positions.get("2")),
                    "single starting position");
            Suite.assertEquals(List.of(0, 1),
                    intList(((Map<?, ?>) search("\"go go\"", true).get("positions")).get("2")),
                    "shorter repeated phrase has two starting positions");

            // Cross-document false match guard: doc2 has "go go go" and doc3
            // has a lone "go". The phrase lives only in doc2 and must never
            // be extended across the document boundary into doc3.
            assertDocIds(List.of(2L), search("\"go go\"", false),
                    "only doc2 contains consecutive go go; doc3's lone go does not");

            // Whitespace handling: double spaces collapse in tokenization.
            assertDocIds(List.of(4L), search("\"hello world\"", false),
                    "hello world across spaces");
            assertDocIds(List.of(), search("\"the brown\"", false),
                    "non-adjacent tokens do not match");
            assertDocIds(List.of(1L), search("\"quick brown\"", false), "quick brown");

            // Boolean combinations.
            assertDocIds(List.of(1L), search("quick AND fox", false), "AND");
            assertDocIds(List.of(3L), search("go AND NOT \"go go\"", false),
                    "doc3 has go but never the longer consecutive phrase");
            assertDocIds(List.of(1L, 4L),
                    search("(brown OR world) AND NOT go", false),
                    "parenthesized boolean");
            assertDocIds(List.of(1L, 2L, 3L, 4L),
                    search("quick OR go OR hello", false), "OR union");

            // Empty document: indexes nothing but participates in NOT.
            // (doc1 "the quick brown fox" lacks all three terms too.)
            put(5, "");
            assertDocIds(List.of(1L, 5L), search("NOT go AND NOT hello", false),
                    "empty doc and unrelated doc match negatives");
            Map<?, ?> doc5 = body(request("GET", "/documents/5", null));
            Suite.assertEquals("", doc5.get("text"), "empty doc text retained");

            // Replace doc2: stale positions must not survive.
            put(2, "zebra zebra");
            assertDocIds(List.of(), search("\"go go go\"", false),
                    "old phrase gone after replacement");
            assertDocIds(List.of(3L), search("go", false),
                    "only doc3 still has go; doc2 go positions removed");
            assertDocIds(List.of(2L), search("zebra", false),
                    "new content indexed");
            assertDocIds(List.of(2L), search("\"zebra zebra\"", false),
                    "new repeated phrase");

            // Bulk replace doc2 with empty text; positions for zebra vanish.
            int bulkStatus = request("POST", "/documents/bulk",
                    "{\"docs\":[{\"id\":2,\"text\":\"\"}]}").statusCode();
            Suite.assertEquals(200, bulkStatus, "bulk status");
            assertDocIds(List.of(), search("zebra", false),
                    "bulk empty replacement clears positions");
            assertDocIds(List.of(2L, 5L),
                    search("NOT go AND NOT quick AND NOT hello AND NOT fox "
                            + "AND NOT brown AND NOT world", false),
                    "doc2 now empty, doc5 empty");

            // GET stored text and 404.
            Suite.assertEquals("the quick brown fox",
                    body(request("GET", "/documents/1", null)).get("text"), "get doc1");
            Suite.assertEquals(404, request("GET", "/documents/999", null).statusCode(),
                    "missing doc 404");

            // Delete removes the document and all positions.
            Suite.assertEquals(200, request("DELETE", "/documents/1", null).statusCode(),
                    "delete ok");
            Suite.assertEquals(404, request("DELETE", "/documents/1", null).statusCode(),
                    "second delete 404");
            assertDocIds(List.of(), search("quick", false),
                    "deleted doc positions gone");

            // Malformed inputs.
            Suite.assertEquals(400, request("POST", "/search",
                    "{\"query\":\"a AND\"}").statusCode(), "parse error -> 400");
            Suite.assertEquals(400, request("POST", "/search",
                    "{\"query\":\"\"}").statusCode(), "empty query -> 400");
            Suite.assertEquals(400, request("POST", "/search", "not json").statusCode(),
                    "invalid json -> 400");
            Suite.assertEquals(400, request("PUT", "/documents/notanid", "x").statusCode(),
                    "bad id -> 400");
            Suite.assertEquals(404, request("GET", "/nope", null).statusCode(),
                    "unknown route -> 404");

            app.stop();
        });

        s.test("HTTP: UTF-8 and case-insensitive keywords/content", () -> {
            startServer();
            put(1, "Hello WORLD");
            assertDocIds(List.of(1L), search("hello world", false),
                    "lowercased matching");
            put(2, "über straße 中文");
            assertDocIds(List.of(2L), search("中文", false), "utf-8 token");
            assertDocIds(List.of(1L), search("world AND NOT 中文", false),
                    "boolean with utf-8");
            app.stop();
        });
    }

    // ---- helpers ----

    private static void startServer() throws Exception {
        app = new HttpServerApp(new InvertedIndex());
        app.start(0);
        root = BASE + app.getPort();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private static void put(int id, String text) throws Exception {
        Suite.assertEquals(200, request("PUT", "/documents/" + id, text).statusCode(),
                "put doc " + id);
    }

    private static void assertDocIds(List<Long> expected, Map<?, ?> response, String message) {
        List<Object> raw = (List<Object>) response.get("docIds");
        Suite.assertEquals(expected, raw.stream().map(o -> ((Number) o).longValue()).toList(),
                message);
    }

    private static List<Integer> intList(Object raw) {
        return ((List<Object>) raw).stream().map(o -> ((Number) o).intValue()).toList();
    }

    private static void assertStatus(int expected, HttpResponse<String> resp) {
        Suite.assertEquals(expected, resp.statusCode(),
                "HTTP status (body=" + resp.body() + ")");
    }

    private static Map<?, ?> search(String query, boolean includePositions) throws Exception {
        String payload = "{\"query\":" + Json.write(query)
                + ",\"includePositions\":" + includePositions + "}";
        HttpResponse<String> resp = request("POST", "/search", payload);
        Suite.assertEquals(200, resp.statusCode(), "search 200 for " + query
                + " body=" + resp.body());
        return (Map<?, ?>) Json.parse(resp.body());
    }

    private static Map<?, ?> body(HttpResponse<String> resp) throws Exception {
        return (Map<?, ?>) Json.parse(resp.body());
    }

    private static HttpResponse<String> request(String method, String path, String body)
            throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(root + path));
        if (body == null) {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            b.header("Content-Type", "text/plain; charset=utf-8");
            b.method(method, HttpRequest.BodyPublishers.ofString(body));
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }
}
