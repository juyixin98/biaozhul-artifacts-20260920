package joinplanner.test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import joinplanner.json.JsonParser;
import joinplanner.web.Server;

/** Boots the real HTTP server and exercises the endpoints over loopback. */
public final class HttpTest {

    public static void run(TestRunner r) throws Exception {
        r.suite("HTTP end-to-end");

        Server server = new Server(0); // ephemeral port
        server.start();
        int port = server.boundPort();
        String base = "http://127.0.0.1:" + port;
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(5)).build();

            // health
            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            r.checkEq("GET /health 200", health.statusCode(), 200);
            r.check("health body", health.body().contains("\"status\": \"ok\""));

            // plan happy path
            String req = """
                    {
                      "tables": [
                        {"name": "orders", "rows": 100000},
                        {"name": "customers", "rows": 10000, "unique": true},
                        {"name": "items", "rows": 500000}
                      ],
                      "edges": [
                        {"leftTable": "orders", "leftColumn": "cust_id",
                         "rightTable": "customers", "rightColumn": "id"},
                        {"leftTable": "items", "leftColumn": "order_id",
                         "rightTable": "orders", "rightColumn": "id"}
                      ]
                    }
                    """;
            HttpResponse<String> plan = post(client, base + "/api/plan", req);
            r.checkEq("POST /api/plan 200", plan.statusCode(), 200);
            Object planJson = JsonParser.parse(plan.body());
            r.check("plan has joinOrder", plan.body().contains("\"joinOrder\""));
            r.check("plan explains assumptions",
                    plan.body().contains("selectivitySource"));

            // disconnected -> 422 with components
            String disc = """
                    {"tables":[{"name":"a","rows":10},{"name":"b","rows":20},
                               {"name":"c","rows":30}],
                     "edges":[{"leftTable":"a","leftColumn":"x",
                               "rightTable":"b","rightColumn":"y"}]}
                    """;
            HttpResponse<String> d422 = post(client, base + "/api/plan", disc);
            r.checkEq("disconnected -> 422", d422.statusCode(), 422);
            r.check("422 names components", d422.body().contains("\"components\""));

            // bad json -> 400
            HttpResponse<String> bad = post(client, base + "/api/plan", "{oops");
            r.checkEq("invalid JSON -> 400", bad.statusCode(), 400);

            // >8 tables -> 400
            StringBuilder nine = new StringBuilder("{\"tables\":[");
            for (int i = 0; i < 9; i++) {
                if (i > 0) nine.append(',');
                nine.append("{\"name\":\"t").append(i).append("\",\"rows\":1}");
            }
            nine.append("]}");
            HttpResponse<String> toobig = post(client, base + "/api/plan", nine.toString());
            r.checkEq("9 tables -> 400", toobig.statusCode(), 400);

            // GET on POST endpoint -> 405
            HttpResponse<String> method = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/api/plan")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            r.checkEq("GET /api/plan -> 405", method.statusCode(), 405);

            // simulate happy path
            String sim = """
                    {
                      "seed": 7,
                      "maxRows": 2000000,
                      "problem": {
                        "tables": [
                          {"name": "a", "rows": 300},
                          {"name": "b", "rows": 500},
                          {"name": "c", "rows": 200}
                        ],
                        "edges": [
                          {"leftTable": "a", "leftColumn": "x",
                           "rightTable": "b", "rightColumn": "y"},
                          {"leftTable": "b", "leftColumn": "z",
                           "rightTable": "c", "rightColumn": "w"}
                        ]
                      },
                      "columns": [
                        {"table": "a", "column": "x", "ndv": 12, "zipf": 1.5},
                        {"table": "b", "column": "y", "ndv": 20, "zipf": 0.2},
                        {"table": "b", "column": "z", "ndv": 8, "zipf": 1.3},
                        {"table": "c", "column": "w", "ndv": 30, "zipf": 0.4}
                      ]
                    }
                    """;
            HttpResponse<String> simResp = post(client, base + "/api/simulate", sim);
            r.checkEq("POST /api/simulate 200", simResp.statusCode(), 200);
            r.check("sim verdict present",
                    simResp.body().contains("\"outcome\""));
            r.check("sim compares cardinalities",
                    simResp.body().contains("\"actualRows\""));
        } finally {
            server.stop();
        }
    }

    private static HttpResponse<String> post(HttpClient client, String url, String body)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
