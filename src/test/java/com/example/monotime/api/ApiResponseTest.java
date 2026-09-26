package com.example.monotime.api;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ApiResponseTest {

    @Test
    void okCarriesDataWithoutError() {
        // Act
        ApiResponse response = ApiResponse.ok("payload");

        // Assert
        assertTrue(response.success());
        assertEquals("payload", response.data());
        assertNull(response.error());
    }

    @Test
    void failureCarriesErrorWithoutData() {
        // Act
        ApiResponse response = ApiResponse.failure("boom");

        // Assert
        assertFalse(response.success());
        assertNull(response.data());
        assertEquals("boom", response.error());
    }

    @Test
    void failureJsonIsWellFormed() throws Exception {
        // Act
        String json = JsonMappers.create().writeValueAsString(ApiResponse.failure("坏了"));

        // Assert
        assertTrue(json.contains("\"success\":false"));
        assertTrue(json.contains("坏了"));
    }
}
