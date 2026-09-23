package com.example.drvb.server;

/** Carries a 4xx-level request error with a stable reason code. */
class BadRequestException extends RuntimeException {

    private final int status;

    BadRequestException(String message) {
        this(400, message);
    }

    BadRequestException(int status, String message) {
        super(message);
        this.status = status;
    }

    int status() {
        return status;
    }
}
