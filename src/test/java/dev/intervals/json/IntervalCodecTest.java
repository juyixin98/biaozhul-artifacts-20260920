package dev.intervals.json;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.intervals.model.Interval;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.ZoneId;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalCodecTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private Interval decode(String intervalJson, Domain domain, String zone) throws Exception {
        JsonNode n = MAPPER.readTree(intervalJson);
        return new IntervalCodec(domain, ZoneId.of(zone)).decode(n);
    }

    @Test
    @DisplayName("时间域：纯日期按 timeZone 零点解释")
    void dateOnlyUsesZoneMidnight() throws Exception {
        Interval sh = decode("{\"lower\":\"2026-01-01\",\"upper\":\"2026-01-02\"}",
                Domain.TIME, "Asia/Shanghai");
        Interval utc = decode("{\"lower\":\"2026-01-01\",\"upper\":\"2026-01-02\"}",
                Domain.TIME, "UTC");
        // 上海零点比 UTC 零点早 8 小时（epoch 毫秒差 -28800000）
        assertEquals(-28800000L, sh.lower().value() - utc.lower().value());
    }

    @Test
    @DisplayName("时间域：带 Z 与带偏移量的时间按绝对时刻解析，结果一致")
    void zonedAndOffsetAgree() throws Exception {
        Interval a = decode("{\"lower\":\"2026-01-01T00:00:00Z\",\"upper\":\"2026-01-02T00:00:00Z\"}",
                Domain.TIME, "UTC");
        Interval b = decode(
                "{\"lower\":\"2026-01-01T08:00:00+08:00\",\"upper\":\"2026-01-02T08:00:00+08:00\"}",
                Domain.TIME, "Asia/Shanghai");
        assertEquals(a, b);
    }

    @Test
    @DisplayName("无穷端点：null / -inf / infinity / * 均可解析")
    void infinityTokens() throws Exception {
        String[][] cases = {
                {"{\"lower\":null,\"upper\":10}", "negNull"},
                {"{\"lower\":\"-inf\",\"upper\":10}", "neg"},
                {"{\"lower\":1,\"upper\":\"+inf\"}", "pos"},
                {"{\"lower\":\"*\",\"upper\":\"*\"}", "star"}
        };
        for (String[] c : cases) {
            Interval i = decode(c[0], Domain.VERSION, "UTC");
            switch (c[1]) {
                case "negNull", "neg" -> assertTrue(i.lower().isNegInf(), c[0]);
                case "pos" -> assertTrue(i.upper().isPosInf(), c[0]);
                case "star" -> {
                    assertTrue(i.lower().isNegInf());
                    assertTrue(i.upper().isPosInf());
                }
                default -> throw new IllegalStateException();
            }
        }
    }

    @Test
    @DisplayName("版本域：数字字符串也可解析为整数")
    void versionNumericString() throws Exception {
        Interval i = decode("{\"lower\":\"5\",\"upper\":\"9\"}", Domain.VERSION, "UTC");
        assertEquals(5, i.lower().value());
        assertEquals(9, i.upper().value());
    }

    @Test
    @DisplayName("编码往返：时间域输出带偏移量字符串，版本域输出整数，无穷输出 null")
    void encodeShapes() {
        IntervalCodec timeCodec = new IntervalCodec(Domain.TIME, ZoneId.of("UTC"));
        assertEquals("2026-01-01T00:00:00Z",
                timeCodec.encode(dev.intervals.model.Endpoint.finite(
                        java.time.Instant.parse("2026-01-01T00:00:00Z").toEpochMilli())));
        assertEquals(null, timeCodec.encode(dev.intervals.model.Endpoint.posInf()));

        IntervalCodec verCodec = new IntervalCodec(Domain.VERSION, ZoneId.of("UTC"));
        assertEquals(42L, verCodec.encode(dev.intervals.model.Endpoint.finite(42)));
        assertEquals(null, verCodec.encode(dev.intervals.model.Endpoint.negInf()));
    }

    @Test
    @DisplayName("Domain.fromString：别名与默认值，非法值抛错")
    void domainParsing() {
        assertEquals(Domain.TIME, Domain.fromString(null));
        assertEquals(Domain.TIME, Domain.fromString("timestamp"));
        assertEquals(Domain.VERSION, Domain.fromString("integer"));
        assertEquals(Domain.VERSION, Domain.fromString("VERSION"));
        assertTrue(org.junit.jupiter.api.Assertions.assertThrows(
                IllegalArgumentException.class, () -> Domain.fromString("float"))
                .getMessage().contains("domain"));
    }
}
