package com.dstexp.json;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

/**
 * Shares a single configured Jackson mapper across the CLI.
 */
public final class JsonMappers {

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .registerModule(new com.fasterxml.jackson.datatype.jsr310.JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);

    private JsonMappers() {
    }

    public static ObjectMapper mapper() {
        return MAPPER;
    }
}
