package partjoin;

/** Error carrying a stable machine-readable code. */
public class JoinException extends RuntimeException {

    public static final String INVALID_REQUEST = "INVALID_REQUEST";
    public static final String IO_ERROR = "IO_ERROR";
    public static final String DISK_BUDGET_EXHAUSTED = "DISK_BUDGET_EXHAUSTED";

    private final String code;

    public JoinException(String code, String message) {
        super(message);
        this.code = code;
    }

    public JoinException(String code, String message, Throwable cause) {
        super(message, cause);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
