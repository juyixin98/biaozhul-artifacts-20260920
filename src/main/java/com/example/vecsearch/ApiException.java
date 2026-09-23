package com.example.vecsearch;

/** Error carrying an HTTP status code, thrown by handlers and service code. */
public class ApiException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final int status;

    public ApiException(int status, String message) {
        super(message);
        this.status = status;
    }

    public int status() {
        return status;
    }
}
