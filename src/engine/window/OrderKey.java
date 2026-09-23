package engine.window;

/**
 * 一个排序键。
 *
 * <p>{@code ascending} 为 false 表示 DESC；{@code nullOrder} 必须显式给出
 * （请求中省略时由解析层按 SQL 默认补齐：ASC → NULLS LAST，DESC → NULLS FIRST）。
 */
public record OrderKey(String column, boolean ascending, NullOrder nullOrder) {
}
