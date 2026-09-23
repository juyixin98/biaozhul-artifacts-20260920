package cdcrebuild.engine;

/** 事件通过了结构校验，但违背状态机语义，如未 BEGIN 的 DATA（HTTP 409）。 */
public class SemanticException extends RuntimeException {
    public SemanticException(String message) {
        super(message);
    }
}
