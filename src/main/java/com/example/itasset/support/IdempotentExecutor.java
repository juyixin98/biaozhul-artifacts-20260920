package com.example.itasset.support;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.function.Supplier;

/**
 * Runs a mutating operation idempotently inside ONE transaction:
 * register/replay the request id, execute the business change, and persist the response
 * atomically. If the business change rolls back, the idempotency marker rolls back too,
 * so the client may safely retry with the same X-Request-Id.
 */
@Service
public class IdempotentExecutor {

    private final IdempotencyService idempotency;
    private final ObjectMapper objectMapper;

    public IdempotentExecutor(IdempotencyService idempotency, ObjectMapper objectMapper) {
        this.idempotency = idempotency;
        this.objectMapper = objectMapper;
    }

    @Transactional
    public <T> Result<T> execute(String requestId, String operation, String payloadJson,
                                 int successStatus, Supplier<T> action) {
        return execute(requestId, operation, payloadJson, successStatus, null, action);
    }

    /**
     * @param requiredRole authority to enforce, e.g. {@code ROLE_FINANCE}; null = any
     *                     authenticated user. Checked before replay so replayed responses
     *                     never bypass authorization.
     */
    @Transactional
    public <T> Result<T> execute(String requestId, String operation, String payloadJson,
                                 int successStatus, String requiredRole, Supplier<T> action) {
        if (requiredRole != null) {
            Authentication auth = SecurityContextHolder.getContext().getAuthentication();
            boolean allowed = auth != null && auth.getAuthorities().stream()
                    .map(GrantedAuthority::getAuthority)
                    .anyMatch(requiredRole::equals);
            if (!allowed) {
                throw new AccessDeniedException("Access denied for the current role");
            }
        }
        IdempotencyOutcome outcome = idempotency.begin(requestId, operation, payloadJson);
        if (outcome.replayed()) {
            return Result.replayed(outcome);
        }
        T body = action.get();
        String json;
        try {
            json = objectMapper.writeValueAsString(body);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
        idempotency.storeResponse(requestId, successStatus, json);
        return Result.success(body, successStatus);
    }

    public record Result<T>(boolean replayed, IdempotencyOutcome replay, T body, int status) {

        static <T> Result<T> replayed(IdempotencyOutcome outcome) {
            return new Result<>(true, outcome, null, outcome.storedStatus());
        }

        static <T> Result<T> success(T body, int status) {
            return new Result<>(false, null, body, status);
        }
    }
}
