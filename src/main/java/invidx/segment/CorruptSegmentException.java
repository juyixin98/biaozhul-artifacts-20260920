package invidx.segment;

/** Thrown when a published segment file is missing or fails verification. */
public class CorruptSegmentException extends RuntimeException {
    public CorruptSegmentException(String segment, String reason) {
        super("corrupt segment " + segment + ": " + reason);
    }
}
