package vecq;

/** 查询计划 / 查询请求本身不合法（列不存在、类型不匹配、计划结构非法等）。映射为 HTTP 400。 */
public final class InvalidQueryException extends RuntimeException {
    public InvalidQueryException(String msg) { super(msg); }
}
