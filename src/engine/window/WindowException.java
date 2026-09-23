package engine.window;

/**
 * 窗口运算错误：请求本身合法，但窗口计算无法完成
 * （典型场景：求和结果超出 64 位有符号整数范围）。
 */
public class WindowException extends RuntimeException {
    public WindowException(String message) {
        super(message);
    }
}
