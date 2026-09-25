package com.example.dedup.service;

import com.example.dedup.json.Json;
import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/** Small helpers for JSON over HTTP. */
final class HttpJson {
    private HttpJson() {
    }

    static void send(HttpExchange ex, int status, Json.Value body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    static Json.Value readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return Json.obj();
        }
        return Json.parse(new String(bytes, StandardCharsets.UTF_8));
    }

    static Json.JsonObject envelope(boolean ok) {
        Json.JsonObject o = Json.obj();
        o.members().put("ok", Json.JsonBool.of(ok));
        return o;
    }
}
