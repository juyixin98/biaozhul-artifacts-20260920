package invidx.engine;

/** Unchecked wrapper for storage I/O failures on the mutation path. */
public class IndexIOException extends RuntimeException {
    public IndexIOException(String message, Throwable cause) {
        super(message, cause);
    }
}
