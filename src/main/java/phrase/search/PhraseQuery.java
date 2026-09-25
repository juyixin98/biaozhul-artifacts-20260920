package phrase.search;

import java.util.List;

/**
 * 短语查询：有序词项 + 固定 slop。
 *
 * <p><b>slop 定义（固定）：</b>slop = 相邻两个匹配词项之间允许出现的
 * “其他词（空位）”的最大数量。要求严格递增位置 p0 &lt; p1 &lt; ... &lt; pn-1，
 * 且对所有 i 有 {@code 1 <= p(i+1)-p(i) <= slop+1}。
 * <ul>
 *   <li>slop=0：相邻精确短语（位置差恰为 1）；</li>
 *   <li>slop=1：相邻词之间至多 1 个其他词；以此类推。</li>
 * </ul>
 * 停用词被删除后留下的空位同样按位置差计入 slop —— 这就是停用词“保留位置”的含义。
 */
public record PhraseQuery(List<String> terms, int slop, String field) {

    /** 每文档最多返回的匹配数量（防止指数级输出打爆响应）。 */
    public static final int MAX_SLOP = 1000;

    public PhraseQuery {
        terms = List.copyOf(terms);
        if (terms.isEmpty()) {
            throw new IllegalArgumentException("query must contain at least one term");
        }
        for (String t : terms) {
            if (t == null || t.isEmpty()) {
                throw new IllegalArgumentException("query term must not be empty");
            }
        }
        if (slop < 0) {
            throw new IllegalArgumentException("slop must be >= 0");
        }
        if (slop > MAX_SLOP) {
            throw new IllegalArgumentException("slop must be <= " + MAX_SLOP);
        }
    }

    public static PhraseQuery of(List<String> terms, int slop) {
        return new PhraseQuery(terms, slop, null);
    }

    public boolean isFieldScoped() {
        return field != null && !field.isEmpty();
    }
}
