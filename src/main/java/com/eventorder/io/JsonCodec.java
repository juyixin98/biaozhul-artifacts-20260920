package com.eventorder.io;

import com.eventorder.model.OrderRequest;
import com.eventorder.model.OrderResult;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import java.io.IOException;

/** JSON (de)serialization boundary. All input validation happens after this layer. */
public final class JsonCodec {

    private final ObjectMapper mapper;

    public JsonCodec() {
        this.mapper = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
    }

    public OrderRequest readRequest(String json) throws IOException {
        return mapper.readValue(json, OrderRequest.class);
    }

    public String writeResult(OrderResult result) throws IOException {
        return mapper.writeValueAsString(result);
    }
}
