package dev.dedup.hll;

/** 草图合并时配置不一致（精度或哈希标识不同）。 */
public class IncompatibleSketchException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public IncompatibleSketchException(String message) {
        super(message);
    }
}
