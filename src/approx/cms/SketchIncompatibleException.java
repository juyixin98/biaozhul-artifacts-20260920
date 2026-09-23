package approx.cms;

/** Thrown when two sketches cannot be merged because their layout differs. */
public class SketchIncompatibleException extends RuntimeException {
    public SketchIncompatibleException(String message) {
        super(message);
    }
}
