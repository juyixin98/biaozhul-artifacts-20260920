package com.example.paginate.web;

import com.example.paginate.json.Json;
import com.sun.net.httpserver.HttpExchange;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/** HTTP 读写工具。 */
public final class HttpSupport {

    private static final int MAX_BODY_BYTES = 1 << 20; // 1 MiB

    private HttpSupport() {
    }

    public static Map<String, Object> readJsonObject(HttpExchange ex) {
        String text = readBody(ex);
        if (text.isEmpty()) {
            throw ApiException.badJson("请求体为空，需要 application/json 对象");
        }
        try {
            return Json.parseObject(text);
        } catch (RuntimeException e) {
            throw ApiException.badJson("请求体不是合法 JSON 对象: " + e.getMessage());
        }
    }

    public static String readBody(HttpExchange ex) {
        try (InputStream in = ex.getRequestBody()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int total = 0;
            int n;
            while ((n = in.read(buf)) != -1) {
                total += n;
                if (total > MAX_BODY_BYTES) {
                    throw ApiException.badRequest("body_too_large", "请求体超过 1 MiB 限制");
                }
                out.write(buf, 0, n);
            }
            return out.toString(StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw ApiException.badJson("读取请求体失败: " + e.getMessage());
        }
    }

    public static void sendJson(HttpExchange ex, int status, Object bodyObj) {
        byte[] body = Json.stringify(bodyObj).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.getResponseHeaders().set("Cache-Control", "no-store");
        try {
            ex.sendResponseHeaders(status, body.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(body);
            }
        } catch (IOException e) {
            // 客户端已断开等，无法继续写响应
            ex.close();
        }
    }
}
