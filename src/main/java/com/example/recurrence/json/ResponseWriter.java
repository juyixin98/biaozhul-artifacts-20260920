package com.example.recurrence.json;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.time.LocalDate;
import java.time.ZoneId;
import java.util.List;

/** Builds stable JSON response documents. */
public final class ResponseWriter {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private ResponseWriter() {
    }

    public static String success(String requestId, List<LocalDate> occurrences) {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("ok", true);
        if (requestId != null) {
            root.put("requestId", requestId);
        }
        root.put("tzdbVersion", tzdbVersion());
        root.put("count", occurrences.size());
        ArrayNode dates = root.putArray("occurrences");
        occurrences.forEach(d -> dates.add(d.toString()));
        return root.toPrettyString();
    }

    public static String error(String requestId, String code, String message) {
        ObjectNode root = MAPPER.createObjectNode();
        root.put("ok", false);
        if (requestId != null) {
            root.put("requestId", requestId);
        }
        root.put("tzdbVersion", tzdbVersion());
        ObjectNode error = root.putObject("error");
        error.put("code", code);
        error.put("message", message);
        return root.toPrettyString();
    }

    /**
     * IANA Time Zone Database version shipped with the running JDK.
     * The built-in {@link java.time.zone.ZoneRulesProvider} keys its single
     * version entry for any zone (e.g. UTC) with the tzdb release name.
     */
    public static String tzdbVersion() {
        try {
            return java.time.zone.ZoneRulesProvider.getVersions("UTC").lastKey();
        } catch (RuntimeException e) {
            return "unknown";
        }
    }

    public static ObjectMapper mapper() {
        return MAPPER;
    }
}
