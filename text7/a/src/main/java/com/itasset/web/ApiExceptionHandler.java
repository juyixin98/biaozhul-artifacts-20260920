package com.itasset.web;

import com.itasset.service.BusinessRuleException;
import com.itasset.service.ConflictException;
import com.itasset.service.NotFoundException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.orm.ObjectOptimisticLockingFailureException;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.OffsetDateTime;
import java.util.Map;
import java.util.stream.Collectors;

@RestControllerAdvice
public class ApiExceptionHandler {

    @ExceptionHandler(NotFoundException.class)
    public ResponseEntity<Map<String, Object>> notFound(NotFoundException e) {
        return build(HttpStatus.NOT_FOUND, e.getMessage());
    }

    @ExceptionHandler({ConflictException.class, ObjectOptimisticLockingFailureException.class})
    public ResponseEntity<Map<String, Object>> conflict(RuntimeException e) {
        String msg = e instanceof ObjectOptimisticLockingFailureException
                ? "并发修改冲突：资产已被其他事务更新，请重新读取后携带新版本号重试" : e.getMessage();
        return build(HttpStatus.CONFLICT, msg);
    }

    @ExceptionHandler(BusinessRuleException.class)
    public ResponseEntity<Map<String, Object>> badRequest(BusinessRuleException e) {
        return build(HttpStatus.BAD_REQUEST, e.getMessage());
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, Object>> validation(MethodArgumentNotValidException e) {
        String detail = e.getBindingResult().getFieldErrors().stream()
                .map(f -> f.getField() + ": " + f.getDefaultMessage())
                .collect(Collectors.joining("; "));
        return build(HttpStatus.BAD_REQUEST, detail);
    }

    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<Map<String, Object>> denied(AccessDeniedException e) {
        return build(HttpStatus.FORBIDDEN, "当前角色无权执行此操作");
    }

    private ResponseEntity<Map<String, Object>> build(HttpStatus status, String message) {
        return ResponseEntity.status(status).body(Map.of(
                "timestamp", OffsetDateTime.now().toString(),
                "status", status.value(),
                "error", status.getReasonPhrase(),
                "message", message == null ? "" : message));
    }
}
