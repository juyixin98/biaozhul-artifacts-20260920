package com.example.qsketch;

import com.example.qsketch.server.HttpServerMain;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** End-to-end HTTP tests against the JDK HttpServer on an ephemeral port. */
class HttpApiTest {

    private static HttpServerMain app;
    private static String base;
    private static HttpClient client;

    @BeforeAll
    static void start() throws IOException {
        app = new HttpServerMain(0);
        app.start();
        base = "http://localhost:" + app.port();
        client = HttpClient.newHttpClient();
    }

    @AfterAll
    static void stop() {
        app.stop();
    }

    private static HttpResponse<String> send(String method, String path, String json)
            throws IOException, InterruptedException {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
        if (json != null) {
            b.header("Content-Type", "application/json")
             .method(method, HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8));
        } else {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> json(String s) {
        return Json.parseObject(s);
    }

    @Test
    void fullLifecycleCreateInsertQueryMergeSerialize() throws Exception {
        // create
        HttpResponse<String> r = send("POST", "/summaries",
                "{\"id\":\"a\",\"eps\":0.01,\"universe\":10000}");
        assertEquals(201, r.statusCode(), r.body());
        Map<String, Object> a = json(r.body());
        assertEquals(0L, ((Number) a.get("count")).longValue());

        assertEquals(409, send("POST", "/summaries",
                "{\"id\":\"a\",\"eps\":0.01,\"universe\":10000}").statusCode());
        assertEquals(400, send("POST", "/summaries",
                "{\"id\":\"bad\",\"eps\":0,\"universe\":10000}").statusCode());

        // insert two shards
        StringBuilder sb = new StringBuilder("{\"values\":[");
        for (int i = 0; i < 5000; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(i * 2); // 0,2,...,9998
        }
        sb.append("]}");
        assertEquals(200, send("POST", "/summaries/a/values", sb.toString()).statusCode());

        assertEquals(201, send("POST", "/summaries",
                "{\"id\":\"b\",\"eps\":0.01,\"universe\":10000}").statusCode());
        sb = new StringBuilder("{\"values\":[");
        for (int i = 0; i < 5000; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(i * 2 + 1); // 1,3,...,9999
        }
        sb.append("]}");
        assertEquals(200, send("POST", "/summaries/b/values", sb.toString()).statusCode());

        // incompatible summary: different eps
        assertEquals(201, send("POST", "/summaries",
                "{\"id\":\"c\",\"eps\":0.05,\"universe\":10000}").statusCode());
        assertEquals(200, send("POST", "/summaries/c/values", "{\"values\":[1,2,3]}")
                .statusCode());

        // merge rejection: incompatible eps
        HttpResponse<String> rej = send("POST", "/summaries/merge",
                "{\"target\":\"a\",\"sources\":[\"c\"]}");
        assertEquals(409, rej.statusCode());
        assertTrue(rej.body().contains("incompatible"));

        // merge rejection: missing summary
        assertEquals(409, send("POST", "/summaries/merge",
                "{\"target\":\"a\",\"sources\":[\"ghost\"]}").statusCode());

        // happy merge
        HttpResponse<String> mr = send("POST", "/summaries/merge",
                "{\"target\":\"a\",\"sources\":[\"b\"]}");
        assertEquals(200, mr.statusCode(), mr.body());
        assertEquals(10000L, ((Number) json(mr.body()).get("count")).longValue());

        // quantile of 0..9999 at 0.5 must come back near 5000
        HttpResponse<String> q = send("GET", "/summaries/a/quantile?q=0.5", null);
        assertEquals(200, q.statusCode(), q.body());
        long v = ((Number) json(q.body()).get("value")).longValue();
        assertTrue(Math.abs(v - 5000) <= 200, "median=" + v);

        // rank query brackets the exact rank (for dense 0..9999, F(x)=x+1)
        HttpResponse<String> rk = send("GET", "/summaries/a/rank?x=4242", null);
        assertEquals(200, rk.statusCode());
        Map<String, Object> rkm = json(rk.body());
        long lo = ((Number) rkm.get("rankLowerBound")).longValue();
        long hi = ((Number) rkm.get("rankUpperBound")).longValue();
        assertTrue(lo <= 4243 && 4243 <= hi, "rank interval [" + lo + "," + hi + "]");

        // bad query params
        assertEquals(400, send("GET", "/summaries/a/quantile?q=1.5", null).statusCode());
        assertEquals(400, send("GET", "/summaries/a/rank?x=999999", null).statusCode());

        // serialize / deserialize
        HttpResponse<String> ser = send("GET", "/summaries/a/serialize", null);
        assertEquals(200, ser.statusCode());
        String b64 = (String) json(ser.body()).get("data");
        Base64.getDecoder().decode(b64);

        HttpResponse<String> d2 = send("POST", "/summaries/deserialize",
                "{\"id\":\"a2\",\"data\":\"" + b64 + "\"}");
        assertEquals(201, d2.statusCode(), d2.body());
        assertEquals(10000L, ((Number) json(d2.body()).get("count")).longValue());

        assertEquals(400, send("POST", "/summaries/deserialize",
                "{\"id\":\"x\",\"data\":\"!!!notbase64!!!\"}").statusCode());
        assertEquals(409, send("POST", "/summaries/deserialize",
                "{\"id\":\"a\",\"data\":\"" + b64 + "\"}").statusCode());

        // list / get / delete
        HttpResponse<String> list = send("GET", "/summaries", null);
        assertEquals(200, list.statusCode());
        List<?> ids = (List<?>) json(list.body()).get("summaries");
        assertTrue(ids.size() >= 4);

        assertEquals(200, send("GET", "/summaries/a2", null).statusCode());
        assertEquals(404, send("GET", "/summaries/nope", null).statusCode());
        assertEquals(200, send("DELETE", "/summaries/a2", null).statusCode());
        assertEquals(404, send("GET", "/summaries/a2", null).statusCode());
        assertEquals(404, send("DELETE", "/summaries/a2", null).statusCode());

        // 404 / 405 / malformed body
        assertEquals(404, send("GET", "/unknown", null).statusCode());
        assertEquals(405, send("PUT", "/summaries/a", null).statusCode());
        assertEquals(400, send("POST", "/summaries", "not json").statusCode());

        System.out.println("[http] full lifecycle OK, median(0..9999)=" + v
                + " rank(4242) interval=[" + lo + "," + hi + "]");
    }
}
