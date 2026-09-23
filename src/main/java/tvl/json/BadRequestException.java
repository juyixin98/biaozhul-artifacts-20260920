package tvl.json;

/** JSON 解析成功但不符合请求/数据的业务结构约束。 */
public class BadRequestException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public BadRequestException(String message) {
        super(message);
    }
}
