package vecq;

/** 选择向量中出现越界（或负数）下标。映射为 HTTP 400。 */
public final class InvalidSelectionException extends RuntimeException {
    public InvalidSelectionException(String msg) { super(msg); }
}
