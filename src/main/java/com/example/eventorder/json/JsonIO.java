package com.example.eventorder.json;

import com.example.eventorder.engine.RequestException;
import com.example.eventorder.model.OrderRequest;
import com.example.eventorder.model.OrderResponse;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.MapperFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;

/**
 * JSON (de)serialization boundary. Unknown request fields are rejected so
 * typos surface instead of being silently ignored.
 */
public final class JsonIO {

    private final ObjectMapper mapper;

    public JsonIO() {
        // Reject e.g. {"id": 42} binding to a String field instead of silently
        // coercing it to "42"; input validation happens at the boundary.
        this.mapper = new ObjectMapper()
                .disable(MapperFeature.ALLOW_COERCION_OF_SCALARS)
                .enable(SerializationFeature.INDENT_OUTPUT);
        mapper.coercionConfigDefaults()
                .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
    }

    public OrderRequest readRequest(String json) {
        try {
            return mapper.readValue(json, OrderRequest.class);
        } catch (JsonProcessingException e) {
            throw new RequestException("invalid request JSON: " + e.getOriginalMessage());
        }
    }

    public String writeResponse(OrderResponse response) {
        try {
            return mapper.writeValueAsString(response);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException("failed to serialize response", e);
        }
    }
}
