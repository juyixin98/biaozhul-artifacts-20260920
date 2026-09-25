package approxheavy.server;

/** 409-style conflict (e.g. incompatible sketch merge). */
public class ConflictException extends RuntimeException {
    public ConflictException(String message) {
        super(message);
    }
}
