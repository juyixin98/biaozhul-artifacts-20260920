package com.example.asset.web;

import com.example.asset.service.ApiException;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;
import java.util.stream.Collectors;

@RestControllerAdvice
public class GlobalExceptionHandler {

    public record ErrorBody(Instant timestamp, int status, String error, String message) {
    }

    @ExceptionHandler(ApiException.class)
    public ResponseEntity<ErrorBody> api(ApiException e) {
        return build(e.status(), e.getMessage());
    }

    /** 乐观锁兜底：正常路径已被悲观锁串行化，此处防御极端竞争。 */
    @ExceptionHandler(OptimisticLockingFailureException.class)
    public ResponseEntity<ErrorBody> optimistic(OptimisticLockingFailureException e) {
        return build(HttpStatus.CONFLICT, "concurrent modification detected, please retry with the latest version");
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ErrorBody> validation(MethodArgumentNotValidException e) {
        String message = e.getBindingResult().getFieldErrors().stream()
                .map(f -> f.getField() + ": " + f.getDefaultMessage())
                .collect(Collectors.joining("; "));
        return build(HttpStatus.BAD_REQUEST, message);
    }

    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ErrorBody> unreadable(HttpMessageNotReadableException e) {
        return build(HttpStatus.BAD_REQUEST, "malformed request body");
    }

    private static ResponseEntity<ErrorBody> build(HttpStatus status, String message) {
        return ResponseEntity.status(status)
                .body(new ErrorBody(Instant.now(), status.value(), status.getReasonPhrase(), message));
    }
}
