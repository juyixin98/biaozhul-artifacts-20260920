package tvl.expr;

/** 表达式相关编译/运行期错误的公共基类，携带从 0 开始的字符偏移位置。 */
public abstract class ExpressionException extends RuntimeException {
    private final int position;

    protected ExpressionException(String message, int position) {
        super(message);
        this.position = position;
    }

    public int getPosition() {
        return position;
    }
}
