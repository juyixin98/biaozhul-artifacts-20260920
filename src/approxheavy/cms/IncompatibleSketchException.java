package approxheavy.cms;

/** Thrown when two sketches with incompatible geometry/seeds are merged. */
public class IncompatibleSketchException extends RuntimeException {
    public IncompatibleSketchException(String message) {
        super(message);
    }
}
