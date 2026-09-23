package ppd;

/** 引擎内部所有可预期错误的基类（类型错误、未解析列等）。 */
public class EngineException extends RuntimeException {
    public EngineException(String message) { super(message); }
}
