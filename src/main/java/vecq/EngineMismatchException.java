package vecq;

/** 向量化引擎与逐行参照结果不一致（内部不变量被破坏，绝不应发生）。HTTP 500。 */
public final class EngineMismatchException extends RuntimeException {
    public EngineMismatchException(String msg) { super(msg); }
}
