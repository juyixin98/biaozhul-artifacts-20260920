package dev.dedup.hll;

/** 草图二进制/JSON 序列化数据损坏或格式不支持。 */
public class SketchFormatException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public SketchFormatException(String message) {
        super(message);
    }

    public SketchFormatException(String message, Throwable cause) {
        super(message, cause);
    }
}
