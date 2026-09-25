package sessions.service;

/** 请求内容非法（缺字段、类型错等），HTTP 层映射为 400。 */
public class BadRequestException extends RuntimeException {
    public BadRequestException(String message) {
        super(message);
    }
}
