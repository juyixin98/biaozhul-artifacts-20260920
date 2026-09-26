package com.example.eventorder.json;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.engine.RequestException;
import com.example.eventorder.model.OrderRequest;
import org.junit.jupiter.api.Test;

class JsonIOTest {

    private final JsonIO jsonIO = new JsonIO();

    @Test
    void parsesMinimalRequest() {
        OrderRequest request = jsonIO.readRequest("""
                {"requestId":"r1","events":[{"id":"a"},{"id":"b"}],
                 "dependencies":[{"before":"a","after":"b"}]}
                """);
        assertEquals("r1", request.requestId());
        assertEquals(2, request.events().size());
        assertEquals("a", request.dependencies().get(0).before());
    }

    @Test
    void rejectsMalformedJson() {
        RequestException e = assertThrows(RequestException.class,
                () -> jsonIO.readRequest("{not json"));
        assertTrue(e.getMessage().contains("invalid request JSON"));
    }

    @Test
    void rejectsUnknownFields() {
        assertThrows(RequestException.class,
                () -> jsonIO.readRequest("{\"events\":[{\"id\":\"a\"}],\"bogus\":1}"));
    }

    @Test
    void rejectsWrongFieldTypes() {
        assertThrows(RequestException.class,
                () -> jsonIO.readRequest("{\"events\":[{\"id\":42}]}"));
    }
}
