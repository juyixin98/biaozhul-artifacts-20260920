package phrase.search;

import java.util.List;

/**
 * 一次短语命中：
 * <ul>
 *   <li>{@code positions}：每个查询词项对应的<b>全局</b>位置（1 基，严格递增）；</li>
 *   <li>{@code fields}：每个位置所属字段（与 positions 一一对应）；</li>
 *   <li>{@code start}/{@code end}：命中在全局序列上的首尾位置；</li>
 *   <li>{@code crossField}：命中是否跨越字段边界。</li>
 * </ul>
 * 注意：重复词的两次匹配使用两个不同位置（positions 严格递增，天然不重用同一位）。
 */
public record Match(List<Integer> positions, List<String> fields, int start, int end,
                    boolean crossField) {}
