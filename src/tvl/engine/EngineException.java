package tvl.engine;

/** 引擎语义错误：未知表/列、非法 limit 等（用户请求问题，非系统故障）。 */
public class EngineException extends RuntimeException {
    public EngineException(String message) {
        super(message);
    }
}
