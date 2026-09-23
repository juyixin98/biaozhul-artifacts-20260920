package com.example.paginate;

import com.example.paginate.json.Json;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/** 测试用 HTTP 客户端：返回状态码与解析后的 JSON。 */
public final class ApiClient {

    public record Response(int status, Map<String, Object> json) {
        public String errorCode() {
            Object v = json.get("error");
            return v == null ? null : String.valueOf(v);
        }

        public String nextCursor() {
            Object v = json.get("nextCursor");
            return v == null ? null : String.valueOf(v);
        }
    }

    private final String base;
    private final HttpClient http;

    public ApiClient(int port) {
        this.base = "http://localhost:" + port;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    public Response get(String pathWithQuery) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + pathWithQuery)).GET().build();
        return send(req);
    }

    public Response listItems(Map<String, String> params) throws Exception {
        StringBuilder qs = new StringBuilder();
        for (Map.Entry<String, String> e : params.entrySet()) {
            if (e.getValue() == null) {
                continue;
            }
            if (!qs.isEmpty()) {
                qs.append('&');
            }
            qs.append(URLEncoder.encode(e.getKey(), StandardCharsets.UTF_8))
                    .append('=')
                    .append(URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8));
        }
        return get("/api/items?" + qs);
    }

    public Response post(String path, Map<String, Object> body) throws Exception {
        return write("POST", path, body);
    }

    public Response patch(String path, Map<String, Object> body) throws Exception {
        return write("PATCH", path, body);
    }

    public Response delete(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .DELETE().build();
        return send(req);
    }

    private Response write(String method, String path, Map<String, Object> body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .method(method, HttpRequest.BodyPublishers.ofString(Json.stringify(body)))
                .build();
        return send(req);
    }

    private Response send(HttpRequest req) throws Exception {
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        Map<String, Object> json;
        if (resp.body().isBlank()) {
            json = new LinkedHashMap<>();
        } else {
            json = Json.parseObject(resp.body());
        }
        return new Response(resp.statusCode(), json);
    }
}
