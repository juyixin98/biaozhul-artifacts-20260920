package tvl.json;

/** JSON 文本格式错误，{@link #position} 为从 0 开始的字符偏移。 */
public class JsonException extends RuntimeException {
    public final int position;

    public JsonException(String message, int position) {
        super(message);
        this.position = position;
    }
}
