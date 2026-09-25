package dev.timeprecision.http;

import dev.timeprecision.json.JsonParser;
import dev.timeprecision.json.JsonValue;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ServerIntegrationTest {

    private static TimePrecisionServer server;
    private static HttpClient client;
    private static String base;

    @BeforeAll
    static void startServer() throws IOException {
        server = TimePrecisionServer.start(0);
        base = "http://127.0.0.1:" + server.port();
        client = HttpClient.newHttpClient();
    }

    @AfterAll
    static void stopServer() {
        server.stop();
    }

    private static JsonValue.Obj post(String path, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + path))
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .header("Content-Type", "application/json")
                .build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return (JsonValue.Obj) JsonParser.parse(response.body());
    }

    private static HttpResponse<String> raw(String method, String path, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(base + path))
                .method(method, body == null
                        ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    private static String str(JsonValue.Obj obj, String key) {
        return ((JsonValue.Str) obj.get(key)).value();
    }

    private static String errorCode(JsonValue.Obj obj) {
        return str((JsonValue.Obj) obj.get("error"), "code");
    }

    @Test
    void convertNegativeMillisToSecondsFloor() throws Exception {
        JsonValue.Obj res = post("/convert",
                "{\"value\":\"-1500\",\"fromUnit\":\"MILLISECONDS\",\"toUnit\":\"SECONDS\",\"rounding\":\"FLOOR\"}");
        assertEquals(new JsonValue.Bool(true), res.get("ok"));
        assertEquals("-2", str(res, "result"));
        assertEquals("SECONDS", str(res, "resultUnit"));
    }

    @Test
    void convertAcceptsIntegerNumberLiteral() throws Exception {
        JsonValue.Obj res = post("/convert",
                "{\"value\":1500,\"fromUnit\":\"ms\",\"toUnit\":\"ns\"}");
        assertEquals("1500000000", str(res, "result"));
    }

    @Test
    void convertRejectsFractionalNumberLiteral() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert",
                "{\"value\":1.5,\"fromUnit\":\"SECONDS\",\"toUnit\":\"MILLISECONDS\"}");
        assertEquals(422, res.statusCode());
        assertEquals("INVALID_VALUE", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void convertOverflowIsReportedNotTruncated() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert",
                "{\"value\":\"9223372036854775807\",\"fromUnit\":\"SECONDS\",\"toUnit\":\"NANOSECONDS\"}");
        assertEquals(422, res.statusCode());
        assertEquals("OVERFLOW", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void convertDefaultsToUnnecessaryRounding() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert",
                "{\"value\":\"2001\",\"fromUnit\":\"MILLISECONDS\",\"toUnit\":\"SECONDS\"}");
        assertEquals(422, res.statusCode());
        assertEquals("ROUNDING_NECESSARY", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void convertRejectsUnknownUnit() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert",
                "{\"value\":\"1\",\"fromUnit\":\"FORTNIGHTS\",\"toUnit\":\"SECONDS\"}");
        assertEquals(422, res.statusCode());
        assertEquals("INVALID_UNIT", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void parseFractionalSecondsText() throws Exception {
        JsonValue.Obj res = post("/parse",
                "{\"text\":\"123.456789012\",\"toUnit\":\"NANOSECONDS\"}");
        assertEquals("123456789012", str(res, "result"));
    }

    @Test
    void parseNegativeFractionalSeconds() throws Exception {
        JsonValue.Obj res = post("/parse",
                "{\"text\":\"-0.0000005\",\"toUnit\":\"MICROSECONDS\",\"rounding\":\"FLOOR\"}");
        assertEquals("-1", str(res, "result"));
    }

    @Test
    void parseRejectsSubNanosecondPrecision() throws Exception {
        HttpResponse<String> res = raw("POST", "/parse",
                "{\"text\":\"1.0000000001\",\"toUnit\":\"NANOSECONDS\"}");
        assertEquals(422, res.statusCode());
        assertEquals("INVALID_PRECISION", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void formatProducesDecimalSecondsText() throws Exception {
        JsonValue.Obj res = post("/format", "{\"value\":\"-1500\",\"unit\":\"MILLISECONDS\"}");
        assertEquals("-1.5", str(res, "text"));
    }

    @Test
    void roundTripViaParseAndFormatIsLossless() throws Exception {
        JsonValue.Obj parsed = post("/parse",
                "{\"text\":\"-9223372036.854775808\",\"toUnit\":\"NANOSECONDS\"}");
        String nanos = str(parsed, "result");
        assertEquals("-9223372036854775808", nanos);
        JsonValue.Obj formatted = post("/format",
                "{\"value\":\"" + nanos + "\",\"unit\":\"NANOSECONDS\"}");
        assertEquals("-9223372036.854775808", str(formatted, "text"));
    }

    @Test
    void metaReportsTzdbVersion() throws Exception {
        HttpResponse<String> res = raw("GET", "/meta", null);
        assertEquals(200, res.statusCode());
        JsonValue.Obj body = (JsonValue.Obj) JsonParser.parse(res.body());
        JsonValue.Obj meta = assertInstanceOf(JsonValue.Obj.class, body.get("meta"));
        String tzdb = str(meta, "tzdbVersion");
        assertFalse(tzdb.isBlank());
    }

    @Test
    void malformedJsonReturnsBadRequest() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert", "{not json");
        assertEquals(400, res.statusCode());
        assertEquals("BAD_REQUEST", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void missingFieldReturnsBadRequest() throws Exception {
        HttpResponse<String> res = raw("POST", "/convert",
                "{\"value\":\"1\",\"fromUnit\":\"SECONDS\"}");
        assertEquals(400, res.statusCode());
        assertEquals("BAD_REQUEST", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void getOnConvertIsMethodNotAllowed() throws Exception {
        HttpResponse<String> res = raw("GET", "/convert", null);
        assertEquals(405, res.statusCode());
        assertEquals("METHOD_NOT_ALLOWED", errorCode((JsonValue.Obj) JsonParser.parse(res.body())));
    }

    @Test
    void everyResponseCarriesMeta() throws Exception {
        JsonValue.Obj res = post("/convert",
                "{\"value\":\"1\",\"fromUnit\":\"SECONDS\",\"toUnit\":\"SECONDS\"}");
        assertTrue(res.has("meta"));
    }
}
