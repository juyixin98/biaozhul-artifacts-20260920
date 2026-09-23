package com.tjoin.service;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/** 对真实监听端口发起 HTTP 请求的端到端测试（JDK HttpURLConnection）。 */
public class JoinHttpServerTest {

    private static String http(String method, int port, String path, String body) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) URI.create("http://localhost:" + port + path)
                .toURL().openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(5000);
        if (body != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        int code = conn.getResponseCode();
        InputStream stream = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        String resp = new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        if (code >= 400) {
            throw new HttpStatusException(code, resp);
        }
        return resp;
    }

    @Test
    public void healthAndJoinOverHttp() throws Exception {
        try (JoinHttpServer server = new JoinHttpServer(0)) {
            server.start();
            int port = server.getPort();

            String health = http("GET", port, "/health", null);
            Map<String, Object> healthJson = Json.parseObject(health);
            assertEquals(Boolean.TRUE, healthJson.get("ok"), "health ok");

            String request = """
                    {
                      "config": {"lowerBound": -1, "upperBound": 1},
                      "steps": [
                        {"type":"event","side":"LEFT","id":"L1","key":"k","ts":10},
                        {"type":"event","side":"RIGHT","id":"R1","key":"k","ts":11}
                      ]
                    }
                    """;
            String resp = http("POST", port, "/join", request);
            Map<String, Object> json = Json.parseObject(resp);
            assertEquals(Boolean.TRUE, json.get("ok"), "join ok");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> results = (List<Map<String, Object>>) json.get("results");
            assertEquals(1, results.size(), "one pair over HTTP");
        }
    }

    @Test
    public void httpErrorCodes() throws Exception {
        try (JoinHttpServer server = new JoinHttpServer(0)) {
            server.start();
            int port = server.getPort();

            // 非法 JSON → 400
            HttpStatusException e1 = expectHttpError(() -> http("POST", port, "/join", "{not json"));
            assertEquals(400, e1.code, "invalid JSON => 400");

            // 缓冲超限 → 422
            String cap = """
                    {
                      "config": {"lowerBound":0,"upperBound":100,"maxBufferedPerSide":1},
                      "steps": [
                        {"type":"event","side":"LEFT","id":"a","key":"k","ts":1},
                        {"type":"event","side":"LEFT","id":"b","key":"k","ts":2}
                      ]
                    }
                    """;
            HttpStatusException e2 = expectHttpError(() -> http("POST", port, "/join", cap));
            assertEquals(422, e2.code, "capacity => 422");
            assertTrue(e2.body.contains("capacity"), "error body explains capacity");

            // 错误方法 → 405
            HttpStatusException e3 = expectHttpError(() -> http("DELETE", port, "/health", null));
            assertEquals(405, e3.code, "method not allowed");
        }
    }

    private interface HttpCall {
        String run() throws IOException;
    }

    private HttpStatusException expectHttpError(HttpCall call) throws IOException {
        try {
            call.run();
        } catch (HttpStatusException e) {
            return e;
        }
        throw new AssertionError("expected HTTP error status");
    }

    private static final class HttpStatusException extends RuntimeException {
        final int code;
        final String body;

        HttpStatusException(int code, String body) {
            super("HTTP " + code + ": " + body);
            this.code = code;
            this.body = body;
        }
    }
}
