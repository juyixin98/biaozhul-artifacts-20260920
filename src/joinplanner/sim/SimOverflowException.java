package joinplanner.sim;

/** Raised when simulated execution materializes more rows than the safety cap. */
public class SimOverflowException extends RuntimeException {

    private static final long serialVersionUID = 1L;
    private final long limit;
    private final String subsetDesc;

    public SimOverflowException(long limit, String subsetDesc) {
        super("simulation aborted: intermediate result for " + subsetDesc
                + " exceeded the safety cap of " + limit + " rows");
        this.limit = limit;
        this.subsetDesc = subsetDesc;
    }

    public long limit() {
        return limit;
    }

    public String subsetDesc() {
        return subsetDesc;
    }
}
