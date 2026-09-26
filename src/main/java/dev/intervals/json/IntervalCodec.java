package dev.intervals.json;

import com.fasterxml.jackson.databind.JsonNode;
import dev.intervals.model.Edge;
import dev.intervals.model.Endpoint;
import dev.intervals.model.Interval;

import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;

/**
 * 区间 JSON 编解码。
 *
 * <p>端点 JSON 约定：
 * <ul>
 *   <li>{@code domain=time}：ISO-8601 字符串；不带偏移量时按请求 timeZone 解释；
 *       {@code null} 或缺省表示该侧无穷；</li>
 *   <li>{@code domain=version}：整数；{@code null} 或缺省表示无穷；</li>
 *   <li>{@code lowerOpen} / {@code upperOpen}：是否开端，缺省 false（闭端）。</li>
 * </ul>
 */
public final class IntervalCodec {

    private final Domain domain;
    private final ZoneId zone;
    private final DateTimeFormatter outputFmt;

    public IntervalCodec(Domain domain, ZoneId zone) {
        this.domain = domain;
        this.zone = zone;
        this.outputFmt = DateTimeFormatter.ISO_OFFSET_DATE_TIME.withZone(zone);
    }

    public Interval decode(JsonNode node) {
        Endpoint lower = readEndpoint(node, "lower", true);
        Endpoint upper = readEndpoint(node, "upper", false);
        Edge left = bool(node, "lowerOpen") ? Edge.OPEN : Edge.CLOSED;
        Edge right = bool(node, "upperOpen") ? Edge.OPEN : Edge.CLOSED;
        return Interval.of(lower, left, upper, right);
    }

    private Endpoint readEndpoint(JsonNode node, String field, boolean lowerSide) {
        if (!node.has(field) || node.get(field).isNull()) {
            return lowerSide ? Endpoint.negInf() : Endpoint.posInf();
        }
        JsonNode v = node.get(field);
        if (v.isNumber()) {
            return Endpoint.finite(v.asLong());
        }
        String text = v.asText().trim();
        if (isInfinity(text)) {
            if (text.startsWith("-") || text.equalsIgnoreCase("-inf")) {
                return Endpoint.negInf();
            }
            if (text.startsWith("+") || text.equalsIgnoreCase("inf")
                    || text.equalsIgnoreCase("infinity")) {
                return Endpoint.posInf();
            }
            return lowerSide ? Endpoint.negInf() : Endpoint.posInf();
        }
        if (domain == Domain.VERSION) {
            return Endpoint.finite(Long.parseLong(text));
        }
        return Endpoint.finite(parseTime(text).toEpochMilli());
    }

    private static boolean isInfinity(String text) {
        return text.equalsIgnoreCase("-inf") || text.equalsIgnoreCase("+inf")
                || text.equalsIgnoreCase("inf") || text.equalsIgnoreCase("infinity")
                || text.equals("-∞") || text.equals("+∞") || text.equals("∞")
                || text.equals("*");
    }

    private Instant parseTime(String text) {
        // 带偏移 / Z：直接解析
        if (text.indexOf('Z') > 0 || text.matches(".*[+-]\\d{2}:?\\d{2}$")) {
            return Instant.parse(text);
        }
        if (text.length() == 10) {
            return LocalDate.parse(text).atStartOfDay(zone).toInstant();
        }
        return LocalDateTime.parse(text).atZone(zone).toInstant();
    }

    private static boolean bool(JsonNode node, String field) {
        return node.has(field) && node.get(field).asBoolean(false);
    }

    public Object encode(Endpoint e) {
        if (e.isInfinite()) {
            return null;
        }
        if (domain == Domain.VERSION) {
            return e.value();
        }
        return outputFmt.format(Instant.ofEpochMilli(e.value()));
    }

    public Domain domain() {
        return domain;
    }

    public ZoneId zone() {
        return zone;
    }
}
