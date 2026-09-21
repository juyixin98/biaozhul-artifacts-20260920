package com.example.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * Stored first response for an idempotent request. A retry with the same X-Request-Id is
 * replayed verbatim; a retry carrying a different payload is rejected with 409.
 */
@Entity
@Table(name = "idempotent_request")
public class IdempotentRequest {

    @Id
    @Column(name = "request_id", length = 64)
    private String requestId;

    @Column(nullable = false, length = 40)
    private String operation;

    @Column(nullable = false, length = 64)
    private String fingerprint;

    @Column(name = "response_code", nullable = false)
    private int responseCode;

    @Column(name = "response_body", nullable = false, columnDefinition = "MEDIUMTEXT")
    private String responseBody;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    public IdempotentRequest() {
    }

    public IdempotentRequest(String requestId, String operation, String fingerprint,
                             int responseCode, String responseBody) {
        this.requestId = requestId;
        this.operation = operation;
        this.fingerprint = fingerprint;
        this.responseCode = responseCode;
        this.responseBody = responseBody;
        this.createdAt = LocalDateTime.now();
    }

    public String getRequestId() {
        return requestId;
    }

    public String getOperation() {
        return operation;
    }

    public String getFingerprint() {
        return fingerprint;
    }

    public int getResponseCode() {
        return responseCode;
    }

    public String getResponseBody() {
        return responseBody;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
