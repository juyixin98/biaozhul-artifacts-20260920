package streammatch.model;

/**
 * 重叠匹配策略（一个 B 可能对多个等待中的 A 成立，一个 A 也可能被多个 B 匹配）。
 *
 * <ul>
 *   <li>{@link #ALL_CANDIDATES}：输出所有满足条件的 (A, B) 对。A 匹配后仍保留，可与
 *       窗口内后续 B 再次匹配，直到超时；多个等待中的 A 也可被同一个 B 分别匹配。
 *       这是缺省策略，语义最简单、结果集确定，便于与参考实现逐对核对。</li>
 *   <li>{@link #SKIP_PAST_LAST}：事件流上的非重叠（skip-till-next-match 风格）策略——
 *       每当 B 产出匹配后，清空所有仍在等待的 A（消费过的起点不再服务于后续匹配）；
 *       C 同样清空等待中的 A。每个 A 至多参与一次匹配。</li>
 * </ul>
 */
public enum MatchPolicy {
    ALL_CANDIDATES,
    SKIP_PAST_LAST
}
