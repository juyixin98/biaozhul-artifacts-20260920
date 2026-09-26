package hlc;

/** Unchecked base type for all HLC error conditions (bad input, overflow, restore failure). */
public class HLCException extends RuntimeException {

    public HLCException(String message) {
        super(message);
    }

    public HLCException(String message, Throwable cause) {
        super(message, cause);
    }
}
