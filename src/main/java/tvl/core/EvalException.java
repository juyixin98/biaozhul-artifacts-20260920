package tvl.core;

/**
 * 求值阶段错误：除零、整数溢出等。
 *
 * 这类错误在执行具体某一行时才会触发；若其所在分支被短路，
 * 则根本不会执行到，也就不会抛出（短路语义的关键测试点）。
 */
public class EvalException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final String code;

    public EvalException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
