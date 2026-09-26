package com.example.vercov.engine;

/**
 * Raised when a version would overlap an existing version of the same
 * priority. Same-priority conflicts are rejected, never silently merged.
 */
public class ConflictException extends RuntimeException {

    public ConflictException(String message) {
        super(message);
    }
}
