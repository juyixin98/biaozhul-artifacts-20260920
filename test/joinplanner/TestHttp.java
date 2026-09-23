package joinplanner;

import joinplanner.http.ApiServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;

/** Boots the real {@link ApiServer} on an ephemeral port and exercises it over HTTP. */
public class TestHttp {

    public static void main(String[] args) throws Exception {
        TestFramework t = new TestFramework();
        ApiServer server = new ApiServer(0);
        server.start();
        int port = server.actualPort();
        HttpClient client = HttpClient.newHttpClient();

        try {
            // health
            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health")).build(),
                    HttpResponse.BodyHandlers.ofString());
            t.eq(health.statusCode(), 200, "health 200");
            t.check(health.body().contains("\"ok\""), "health body reports ok");

            // plan success
            String planBody = """
                    {"tables":[{"name":"A","rows":100},{"name":"B","rows":1000}],
                     "edges":[{"left":"A","right":"B","selectivity":0.05}]}""";
            HttpResponse<String> plan = post(client, port, "/plan", planBody);
            t.eq(plan.statusCode(), 200, "plan 200");
            t.check(plan.body().contains("optimalPlan"), "plan body has optimalPlan");

            // malformed JSON -> 400
            HttpResponse<String> bad = post(client, port, "/plan", "{not json");
            t.eq(bad.statusCode(), 400, "malformed JSON -> 400");
            t.check(bad.body().contains("MALFORMED_JSON"), "malformed error code");

            // schema violation -> 400
            HttpResponse<String> schema = post(client, port, "/plan",
                    "{\"tables\":[{\"name\":\"A\"}]}");
            t.eq(schema.statusCode(), 400, "schema violation -> 400");

            // 9 tables -> 400
            StringBuilder nine = new StringBuilder("{\"tables\":[");
            for (int i = 0; i < 9; i++) {
                if (i > 0) nine.append(',');
                nine.append("{\"name\":\"T").append(i).append("\",\"rows\":10}");
            }
            nine.append("]}");
            HttpResponse<String> tooMany = post(client, port, "/plan", nine.toString());
            t.eq(tooMany.statusCode(), 400, "9 tables -> 400");

            // disconnected -> 422
            HttpResponse<String> disc = post(client, port, "/plan",
                    "{\"tables\":[{\"name\":\"A\",\"rows\":1},{\"name\":\"B\",\"rows\":1},"
                            + "{\"name\":\"C\",\"rows\":1}],\"edges\":[]}");
            t.eq(disc.statusCode(), 422, "disconnected -> 422");
            t.check(disc.body().contains("DISCONNECTED_GRAPH"), "disconnected body");

            // enumerate + simulate
            HttpResponse<String> en = post(client, port, "/enumerate", planBody);
            t.eq(en.statusCode(), 200, "enumerate 200");
            t.check(en.body().contains("legalLeftDeepOrders"), "enumerate body");

            String simBody = """
                    {"seed":3,
                     "tables":[
                       {"name":"A","rows":2000,"columns":[{"distribution":"uniform","ndv":100}]},
                       {"name":"B","rows":2000,"columns":[{"distribution":"uniform","ndv":100}]}],
                     "edges":[{"left":"A","right":"B","leftColumn":0,"rightColumn":0}]}""";
            HttpResponse<String> sim = post(client, port, "/simulate", simBody);
            t.eq(sim.statusCode(), 200, "simulate 200");
            t.check(sim.body().contains("actualOptimal"), "simulate body");

            // wrong method -> 405
            HttpResponse<String> method = client.send(
                    HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/plan"))
                            .DELETE().build(),
                    HttpResponse.BodyHandlers.ofString());
            t.eq(method.statusCode(), 405, "wrong method -> 405");

        } finally {
            server.stop();
        }

        if (t.failures.isEmpty()) {
            System.out.println("PASS TestHttp (" + t.checks() + " checks)");
        } else {
            System.out.println("FAIL TestHttp");
            t.failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
    }

    private static HttpResponse<String> post(HttpClient client, int port, String path, String body)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
