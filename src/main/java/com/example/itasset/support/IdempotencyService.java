package com.example.itasset.support;

import com.example.itasset.domain.IdempotentRequest;
import com.example.itasset.repo.IdempotentRequestRepository;
import com.example.itasset.web.ApiException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

/**
 * Idempotency for mutating requests, keyed by the {@code X-Request-Id} header.
 *
 * <p>{@link #begin} joins the caller's transaction. The stored row therefore commits
 * together with the business change, and rolls back with it: a failed business call
 * leaves no idempotency marker and is safe to retry. A duplicate request id carrying a
 * different payload is rejected with 409; an identical duplicate returns the stored
 * first response verbatim.
 */
@Service
public class IdempotencyService {

    public static final String HEADER = "X-Request-Id";

    private final IdempotentRequestRepository repository;

    public IdempotencyService(IdempotentRequestRepository repository) {
        this.repository = repository;
    }

    @Transactional(propagation = Propagation.REQUIRED)
    public IdempotencyOutcome begin(String requestId, String operation, String payloadJson) {
        if (requestId == null || requestId.isBlank()) {
            throw ApiException.badRequest("Missing required header X-Request-Id");
        }
        String fingerprint = sha256(operation + "|" + payloadJson);
        IdempotentRequest existing = repository.findById(requestId).orElse(null);
        if (existing != null) {
            if (!existing.getFingerprint().equals(fingerprint)) {
                throw ApiException.conflict(
                        "X-Request-Id was already used with a different request payload");
            }
            return new IdempotencyOutcome(true, existing.getResponseCode(), existing.getResponseBody());
        }
        // Placeholder row makes two in-flight identical requests collide on the PK.
        repository.saveAndFlush(new IdempotentRequest(requestId, operation, fingerprint, 200, ""));
        return new IdempotencyOutcome(false, 200, "");
    }

    @Transactional(propagation = Propagation.REQUIRED)
    public void storeResponse(String requestId, int status, String responseJson) {
        IdempotentRequest row = repository.findById(requestId)
                .orElseThrow(() -> new IllegalStateException("idempotency row vanished for " + requestId));
        IdempotentRequest updated = new IdempotentRequest(requestId, row.getOperation(), row.getFingerprint(),
                status, responseJson == null ? "" : responseJson);
        repository.save(updated);
    }

    public static String sha256(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(input.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
