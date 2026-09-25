package booleansearch.json;

/** JSON 语法错误，携带 0 基字符位置。 */
public class JsonParseException extends RuntimeException {

    private final int position;

    public JsonParseException(String message, int position) {
        super(message + "（位置 " + position + "）");
        this.position = position;
    }

    public int position() {
        return position;
    }
}
