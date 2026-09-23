package io.example.orderedcommit;

/** Thrown when an API request is invalid. Carries the HTTP status to return. */
public class EventException extends RuntimeException {
    private static final long serialVersionUID = 1L;
    private final int httpStatus;

    public EventException(int httpStatus, String message) {
        super(message);
        this.httpStatus = httpStatus;
    }

    public int httpStatus() {
        return httpStatus;
    }

    static EventException badRequest(String message) {
        return new EventException(400, message);
    }

    static EventException notFound(String message) {
        return new EventException(404, message);
    }

    static EventException conflict(String message) {
        return new EventException(409, message);
    }

    static EventException tooManyRequests(String message) {
        return new EventException(429, message);
    }
}
