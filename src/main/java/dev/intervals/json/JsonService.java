package dev.intervals.json;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.intervals.domain.IntervalAlgebra;
import dev.intervals.model.Interval;
import dev.intervals.model.IntervalSet;
import dev.intervals.tz.TzVersion;

import java.time.ZoneId;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON 请求分发：解析请求 → 执行代数运算 → 构造稳定排序的 JSON 响应。
 *
 * <p>请求格式见 README。支持 operation：
 * {@code union}、{@code intersection}、{@code difference}、{@code complement}、{@code all}。
 */
public final class JsonService {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    public String handle(String requestJson) {
        ObjectNode root = JsonNodeFactory.instance.objectNode();
        try {
            JsonNode req = MAPPER.readTree(requestJson);

            Domain domain = Domain.fromString(text(req, "domain", "time"));
            String zoneId = text(req, "timeZone", "Asia/Shanghai");
            ZoneId zone;
            try {
                zone = ZoneId.of(zoneId);
            } catch (Exception e) {
                throw new IllegalArgumentException("非法 timeZone: " + zoneId);
            }
            IntervalCodec codec = new IntervalCodec(domain, zone);

            String op = text(req, "operation", "union");
            IntervalSet a = decodeSet(req.path("a"), codec);
            IntervalSet b = decodeSet(req.path("b"), codec);

            root.put("ok", true);
            root.put("domain", domain.name().toLowerCase());
            root.put("timeZone", zone.getId());
            root.put("operation", op);
            root.set("tzVersion", MAPPER.valueToTree(TzVersion.info()));

            switch (op) {
                case "union" -> root.set("result", encode(IntervalAlgebra.union(a, b), codec));
                case "intersection" ->
                        root.set("result", encode(IntervalAlgebra.intersection(a, b), codec));
                case "difference" ->
                        root.set("result", encode(IntervalAlgebra.difference(a, b), codec));
                case "complement" ->
                        root.set("result", encode(IntervalAlgebra.complement(a), codec));
                case "all" -> {
                    Map<String, IntervalSet> all = new LinkedHashMap<>();
                    all.put("union", IntervalAlgebra.union(a, b));
                    all.put("intersection", IntervalAlgebra.intersection(a, b));
                    all.put("difference", IntervalAlgebra.difference(a, b));
                    all.put("complementOfA", IntervalAlgebra.complement(a));
                    ObjectNode results = root.putObject("results");
                    all.forEach((k, v) -> results.set(k, encode(v, codec)));
                }
                default -> throw new IllegalArgumentException(
                        "未知 operation: " + op
                                + "（支持 union/intersection/difference/complement/all）");
            }
            return MAPPER.writeValueAsString(root);
        } catch (IllegalArgumentException e) {
            return error(e.getMessage());
        } catch (Exception e) {
            return error("请求解析失败: " + e.getMessage());
        }
    }

    private static IntervalSet decodeSet(JsonNode arr, IntervalCodec codec) {
        List<Interval> list = new ArrayList<>();
        if (arr.isArray()) {
            for (JsonNode n : arr) {
                list.add(codec.decode(n));
            }
        }
        return IntervalSet.of(list);
    }

    private static ArrayNode encode(IntervalSet set, IntervalCodec codec) {
        ArrayNode arr = JsonNodeFactory.instance.arrayNode();
        for (Interval i : set.intervals()) {
            ObjectNode o = arr.addObject();
            o.set("lower", MAPPER.valueToTree(codec.encode(i.lower())));
            o.set("upper", MAPPER.valueToTree(codec.encode(i.upper())));
            o.put("lowerOpen", i.leftEdge() == dev.intervals.model.Edge.OPEN);
            o.put("upperOpen", i.rightEdge() == dev.intervals.model.Edge.OPEN);
            if (i.isSingleton()) {
                o.put("singleton", true);
            }
        }
        return arr;
    }

    private static String error(String message) {
        ObjectNode root = JsonNodeFactory.instance.objectNode();
        root.put("ok", false);
        root.put("error", message);
        return root.toString();
    }

    private static String text(JsonNode node, String field, String fallback) {
        JsonNode v = node.get(field);
        return v == null || v.isNull() ? fallback : v.asText();
    }
}
