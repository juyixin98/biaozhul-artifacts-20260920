package booleansearch.search;

import booleansearch.index.InvertedIndex;
import booleansearch.query.QueryNode;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.TreeSet;

/**
 * 优化求值器（"优化后"）：
 *
 * <p>唯一的查询改写是——对每个 AND 节点的合取项，<b>按倒排链（结果集）大小
 * 从小到大</b>排序后再依次交集。理由：交集循环"遍历累加器、探测右侧集合"，
 * 先拿最小的集合做累加器，能把后续每一轮的探测次数压到最低。
 *
 * <p>基数估计方式（不提前求值、不浪费工作）：
 * <ul>
 *   <li>词项叶子 {@link QueryNode.Term}：直接用 {@link InvertedIndex#postingsSize}，
 *       这就是题目要求的"按倒排大小"；</li>
 *   <li>其他子表达式（括号 / OR / NOT）：先求值得真实大小，再参与排序。</li>
 * </ul>
 * 估计值相同按原顺序稳定排序，保证计划可复现。OR、NOT 语义与朴素版完全一致。
 */
public final class OptimizedPlanner extends NaiveEvaluator {

    public OptimizedPlanner(InvertedIndex index) {
        super(index);
    }

    @Override
    protected TreeSet<Integer> evalAnd(QueryNode.And a) {
        log("AND 优化器：先估计各合取项大小再排序");
        depth++;

        List<Conjunct> conjuncts = new ArrayList<>();
        for (int originalOrder = 0; originalOrder < a.children().size(); originalOrder++) {
            QueryNode child = a.children().get(originalOrder);
            long estimated;
            TreeSet<Integer> materialized = null;
            String source;
            if (child instanceof QueryNode.Term term) {
                estimated = index.postingsSize(term.term());
                source = "倒排大小";
            } else {
                materialized = eval(child);
                estimated = materialized.size();
                source = "子表达式结果大小";
            }
            conjuncts.add(new Conjunct(child, originalOrder, estimated, source, materialized));
        }

        List<Conjunct> ordered = new ArrayList<>(conjuncts);
        ordered.sort(Comparator.comparingLong(Conjunct::estimatedSize)
                .thenComparingInt(Conjunct::originalOrder));

        for (int i = 0; i < ordered.size(); i++) {
            Conjunct c = ordered.get(i);
            String label = describe(c.node());
            log("排序后第 " + (i + 1) + " 位: " + label
                    + "（" + c.estimatedSource() + "=" + c.estimatedSize()
                    + (c.originalOrder() != i ? "，原顺序第 " + (c.originalOrder() + 1 + " 位") : "")
                    + "）");
        }

        TreeSet<Integer> acc = materialize(ordered.get(0));
        for (int i = 1; i < ordered.size(); i++) {
            TreeSet<Integer> right = materialize(ordered.get(i));
            long before = membershipProbes;
            TreeSet<Integer> intersected = intersect(acc, right);
            log("交集: " + acc.size() + " ∩ " + right.size() + "，探测 "
                    + (membershipProbes - before) + " 次 -> " + intersected.size() + " 篇");
            acc = intersected;
            if (acc.isEmpty() && i < ordered.size() - 1) {
                log("中间结果已为空，剩余 " + (ordered.size() - 1 - i)
                        + " 个合取项仍会按计划求值以完整展示，但交集保持空");
            }
        }
        depth--;
        return acc;
    }

    /** 词项此时才真正读取倒排链；其他子表达式已在估计阶段求过值，直接复用。 */
    private TreeSet<Integer> materialize(Conjunct c) {
        if (c.materialized() != null) {
            return c.materialized();
        }
        return eval(c.node());
    }

    private static String describe(QueryNode node) {
        return switch (node) {
            case QueryNode.Term t -> t.term();
            case QueryNode.Not ignored -> "NOT(...)";
            case QueryNode.And ignored -> "( ... AND ... )";
            case QueryNode.Or ignored -> "( ... OR ... )";
        };
    }

    /** 合取项的排序信息载体。 */
    private record Conjunct(QueryNode node,
                            int originalOrder,
                            long estimatedSize,
                            String estimatedSource,
                            TreeSet<Integer> materialized) {
    }
}
