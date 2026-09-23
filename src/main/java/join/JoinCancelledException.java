package join;

/** Thrown from worker loops after a client requests cancellation. */
public final class JoinCancelledException extends Exception {

    private static final long serialVersionUID = 1L;

    public JoinCancelledException(String message) {
        super(message);
    }
}
