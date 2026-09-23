package io.example.orderedcommit;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;

/** Boots a real {@link HttpEventServer} on an ephemeral port and drives it over HTTP. */
final class HttpFixture implements AutoCloseable {

    final int port;
    private final HttpEventServer server;
    private final HttpClient client =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    private HttpFixture(HttpEventServer server) {
        this.server = server;
        this.port = server.port();
    }

    static HttpFixture start(OrderedEventService.Config config) throws Exception {
        OrderedEventService service =
                new OrderedEventService(config, new SleepingEventProcessor());
        HttpEventServer server = new HttpEventServer(service, 8);
        server.start(0, "127.0.0.1"); // ephemeral port
        return new HttpFixture(server);
    }

    record Response(int status, Map<String, Object> body) {
    }

    Response request(String method, String path, String jsonBody) throws Exception {
        HttpRequest.Builder b =
                HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path));
        if (jsonBody != null) {
            b.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        HttpResponse<String> response =
                client.send(b.build(), HttpResponse.BodyHandlers.ofString());
        Map<String, Object> body =
                response.body().isEmpty() ? Map.of() : Json.readObject(response.body());
        return new Response(response.statusCode(), body);
    }

    Response get(String path) throws Exception {
        return request("GET", path, null);
    }

    Response post(String path, String jsonBody) throws Exception {
        return request("POST", path, jsonBody);
    }

    Response put(String path, String jsonBody) throws Exception {
        return request("PUT", path, jsonBody);
    }

    @Override
    public void close() {
        server.stop();
    }
}
