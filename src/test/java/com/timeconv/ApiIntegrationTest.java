package com.timeconv;

import com.timeconv.http.Server;
import com.timeconv.json.Json;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import static com.timeconv.TestRunner.assertEquals;
import static com.timeconv.TestRunner.assertTrue;
import static com.timeconv.TestRunner.test;

/** End-to-end tests over real HTTP against a server on an ephemeral port. */
public final class ApiIntegrationTest {

    public static void register(Path samplesDir) {
        Server server = new Server(0);
        server.start();
        try {
            String base = "http://127.0.0.1:" + server.port();
            HttpClient client = HttpClient.newHttpClient();

            test("GET /meta reports tzdb version and capabilities", () -> {
                Map<String, Object> m = Json.parseObject(get(client, base + "/meta"));
                assertEquals(Boolean.TRUE, m.get("ok"));
                assertTrue(m.get("tzdbVersion") instanceof String s && s.matches("\\d{4}[a-z]"),
                        "tzdbVersion looks like an IANA release: " + m.get("tzdbVersion"));
            });

            test("POST /convert with sample request file", () -> {
                String body = read(samplesDir.resolve("convert-request.json"));
                Map<String, Object> m = Json.parseObject(post(client, base + "/convert", body));
                assertEquals(Boolean.TRUE, m.get("ok"));
                assertEquals("-2", m.get("result"));
                assertEquals("SECOND", m.get("unit"));
                assertEquals(Boolean.FALSE, m.get("exact"));
            });

            test("POST /parse with sample request file", () -> {
                String body = read(samplesDir.resolve("parse-request.json"));
                Map<String, Object> m = Json.parseObject(post(client, base + "/parse", body));
                assertEquals(Boolean.TRUE, m.get("ok"));
                assertEquals("-500", m.get("result"));
                assertEquals(Boolean.TRUE, m.get("exact"));
            });

            test("POST /convert overflow returns OVERFLOW error, HTTP 400", () -> {
                String body = "{\"value\":\"9223372036854775807\",\"fromUnit\":\"SECOND\",\"toUnit\":\"NANOSECOND\"}";
                HttpResponse<String> r = send(client, base + "/convert", body);
                assertEquals(400, r.statusCode());
                Map<String, Object> m = Json.parseObject(r.body());
                assertEquals(Boolean.FALSE, m.get("ok"));
                @SuppressWarnings("unchecked")
                Map<String, Object> err = (Map<String, Object>) m.get("error");
                assertEquals("OVERFLOW", err.get("code"));
            });

            test("POST /convert inexact without rounding returns INEXACT", () -> {
                String body = "{\"value\":\"1500\",\"fromUnit\":\"MILLISECOND\",\"toUnit\":\"SECOND\"}";
                Map<String, Object> m = Json.parseObject(post(client, base + "/convert", body));
                assertEquals(Boolean.FALSE, m.get("ok"));
                @SuppressWarnings("unchecked")
                Map<String, Object> err = (Map<String, Object>) m.get("error");
                assertEquals("INEXACT", err.get("code"));
            });

            test("POST /convert invalid unit returns INVALID_UNIT", () -> {
                String body = "{\"value\":\"1\",\"fromUnit\":\"PICOSECOND\",\"toUnit\":\"SECOND\"}";
                Map<String, Object> m = Json.parseObject(post(client, base + "/convert", body));
                @SuppressWarnings("unchecked")
                Map<String, Object> err = (Map<String, Object>) m.get("error");
                assertEquals("INVALID_UNIT", err.get("code"));
            });

            test("malformed JSON body returns BAD_REQUEST", () -> {
                Map<String, Object> m = Json.parseObject(post(client, base + "/convert", "{nope"));
                @SuppressWarnings("unchecked")
                Map<String, Object> err = (Map<String, Object>) m.get("error");
                assertEquals("BAD_REQUEST", err.get("code"));
            });

            test("GET on /convert is METHOD_NOT_ALLOWED", () -> {
                HttpResponse<String> r;
                try {
                    r = HttpClient.newHttpClient().send(
                            HttpRequest.newBuilder(URI.create(base + "/convert")).GET().build(),
                            HttpResponse.BodyHandlers.ofString());
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new RuntimeException(e);
                }
                assertEquals(405, r.statusCode());
            });
        } finally {
            server.close();
        }
    }

    private static String get(HttpClient client, String url) {
        try {
            return client.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                    HttpResponse.BodyHandlers.ofString()).body();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private static String post(HttpClient client, String url, String body) {
        return send(client, url, body).body();
    }

    private static HttpResponse<String> send(HttpClient client, String url, String body) {
        try {
            return client.send(HttpRequest.newBuilder(URI.create(url))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                    HttpResponse.BodyHandlers.ofString());
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private static String read(Path file) {
        try {
            return Files.readString(file);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private ApiIntegrationTest() {
    }
}
