package com.example.dstexpand.json;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;

/**
 * Shared Jackson configuration: ISO-8601 strings for java.time types,
 * no timestamps, unknown request fields rejected so typos fail fast.
 */
public final class Json {

    private static final ObjectMapper MAPPER = create();

    private Json() {
    }

    public static ObjectMapper mapper() {
        return MAPPER;
    }

    private static ObjectMapper create() {
        ObjectMapper mapper = new ObjectMapper();
        mapper.registerModule(new JavaTimeModule());
        mapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        mapper.disable(SerializationFeature.WRITE_DATE_TIMESTAMPS_AS_NANOSECONDS);
        mapper.disable(DeserializationFeature.READ_DATE_TIMESTAMPS_AS_NANOSECONDS);
        mapper.enable(SerializationFeature.INDENT_OUTPUT);
        // FAIL_ON_UNKNOWN_PROPERTIES stays enabled: misspelled request fields
        // are rejected instead of silently ignored.
        return mapper;
    }
}
