package com.example.window;

import com.sun.net.httpserver.HttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/** Black-box tests for the HTTP API: starts the real server on an ephemeral port. */
final class HttpApiTest {

    private HttpApiTest() {
    }

    private static final class Resp {
        final int status;
        final String body;

        Resp(int status, String body) {
            this.status = status;
            this.body = body;
        }
    }

    static void run() throws Exception {
        System.out.println("HttpApiTest");

        WindowEngine engine = new WindowEngine(10, 2);
        HttpServer server = WindowServer.start(engine, 0);
        HttpClient client = HttpClient.newHttpClient();
        String base = "http://127.0.0.1:" + server.getAddress().getPort();

        try {
            // batch event ingestion
            Resp r = post(client, base + "/api/events",
                    "[{\"id\":\"e1\",\"key\":\"a\",\"eventTime\":3,\"partition\":\"p1\"},"
                            + "{\"id\":\"e2\",\"key\":\"b\",\"eventTime\":7,\"partition\":\"p2\"}]");
            TestMain.eq("events -> 200", 200, r.status);
            List<Object> statuses = Json.asArray(
                    Json.asObject(Json.parse(r.body)).get("results"));
            TestMain.eq("two per-event statuses", 2, statuses.size());
            TestMain.eq("e1 accepted", "ACCEPTED",
                    Json.asObject(statuses.get(0)).get("status"));
            TestMain.eq("e2 window [0,10)", 0L,
                    (Long) Json.asObject(statuses.get(1)).get("windowStart"));

            // min-over-active-partitions watermark
            r = post(client, base + "/api/watermark",
                    "{\"partition\":\"p1\",\"watermark\":10}");
            TestMain.eq("wm p1 -> 200", 200, r.status);
            TestMain.eq("global still null (p2 at min)", null,
                    Json.asObject(Json.parse(r.body)).get("globalWatermark"));

            r = post(client, base + "/api/watermark",
                    "{\"partition\":\"p2\",\"watermark\":10}");
            TestMain.eq("global=10", 10L,
                    Json.asObject(Json.parse(r.body)).get("globalWatermark"));

            r = get(client, base + "/api/results");
            List<Object> wins = Json.asArray(
                    Json.asObject(Json.parse(r.body)).get("results"));
            TestMain.eq("one closed window", 1, wins.size());
            TestMain.eq("closed count = 2", 2L,
                    (Long) Json.asObject(wins.get(0)).get("count"));

            // duplicate
            r = post(client, base + "/api/events",
                    "{\"id\":\"e1\",\"key\":\"a\",\"eventTime\":3,\"partition\":\"p1\"}");
            TestMain.eq("duplicate -> 200", 200, r.status);
            TestMain.eq("duplicate status", "DUPLICATE",
                    Json.asObject(Json.asArray(
                            Json.asObject(Json.parse(r.body)).get("results")).get(0)).get("status"));

            // error cases
            r = post(client, base + "/api/watermark",
                    "{\"partition\":\"p1\",\"watermark\":5}");
            TestMain.eq("watermark regression -> 400", 400, r.status);

            r = post(client, base + "/api/events", "{\"id\":\"x\"}");
            TestMain.eq("missing event fields -> 400", 400, r.status);

            r = post(client, base + "/api/events", "{oops");
            TestMain.eq("malformed JSON -> 400", 400, r.status);

            r = post(client, base + "/api/events", "[]");
            TestMain.eq("empty array -> 200", 200, r.status);

            // idle marking + state snapshot
            r = post(client, base + "/api/partitions/idle",
                    "{\"partition\":\"p1\"}");
            TestMain.eq("idle -> 200", 200, r.status);

            r = get(client, base + "/api/state");
            TestMain.eq("state -> 200", 200, r.status);
            Map<String, Object> state = Json.asObject(Json.parse(r.body));
            TestMain.eq("state windowSize", 10L, state.get("windowSize"));
            TestMain.eq("state allowedLateness", 2L, state.get("allowedLateness"));
            TestMain.eq("state globalWatermark", 10L, state.get("globalWatermark"));
            TestMain.eq("state p1 idle", Boolean.TRUE,
                    Json.asObject(Json.asObject(state.get("partitions")).get("p1")).get("idle"));
            TestMain.eq("state has stats", 2L,
                    (Long) Json.asObject(state.get("stats")).get("accepted"));

            // method / routing
            r = get(client, base + "/api/events");
            TestMain.eq("GET on POST endpoint -> 405", 405, r.status);
            r = get(client, base + "/api/nonexistent");
            TestMain.eq("unknown path -> 404", 404, r.status);
            r = get(client, base + "/");
            TestMain.eq("index -> 200", 200, r.status);

            // reset
            r = post(client, base + "/api/reset", "");
            TestMain.eq("reset -> 200", 200, r.status);
            r = get(client, base + "/api/results");
            TestMain.eq("results cleared", 0,
                    Json.asArray(Json.asObject(Json.parse(r.body)).get("results")).size());
            r = get(client, base + "/api/state");
            TestMain.eq("watermark cleared", null,
                    Json.asObject(Json.parse(r.body)).get("globalWatermark"));
        } finally {
            server.stop(0);
        }
    }

    private static Resp get(HttpClient client, String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return new Resp(resp.statusCode(), resp.body());
    }

    private static Resp post(HttpClient client, String url, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return new Resp(resp.statusCode(), resp.body());
    }
}
