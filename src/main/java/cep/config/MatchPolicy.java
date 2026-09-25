package cep.config;

/**
 * 一个 B 到达时，如何在仍然有效的多个 A 候选之间选择。
 *
 * <ul>
 *   <li>{@link #ALL_PAIRS}：与每个有效 A 各产出一个匹配（重叠匹配全部保留）；</li>
 *   <li>{@link #EARLIEST_A}：仅与全序最靠前（最早）的有效 A 匹配；</li>
 *   <li>{@link #LATEST_A}：仅与全序最靠后（最晚）的有效 A 匹配；</li>
 *   <li>{@link #NON_OVERLAPPING}：贪婪地与最早的有效 A 匹配，且消费区间
 *       {@code [A .. B]} 内所有 A，被消费的 A 不再参与后续匹配（不重叠）。</li>
 * </ul>
 *
 * <p>无论哪种策略，被匹配过的 A 都不再对后续 B 生效；其超时也不会发出
 * （NON_OVERLAPPING 下被同一匹配"顺带消费"的 A 同样不超时）。
 */
public enum MatchPolicy {
    ALL_PAIRS,
    EARLIEST_A,
    LATEST_A,
    NON_OVERLAPPING
}
