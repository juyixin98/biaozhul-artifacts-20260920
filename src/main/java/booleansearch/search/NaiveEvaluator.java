package booleansearch.search;

import booleansearch.index.InvertedIndex;
import booleansearch.query.QueryNode;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeSet;

/**
 * 朴素求值器：严格按解析得到的语法树左到右、左结合求值。
 *
 * <p>AND 交集与 OR 并集都不调整操作数顺序，作为"优化前"的对照基线。
 * 交集采用"遍历累加器、对右侧集合做 contains 探测"的方式，
 * 这样不同顺序产生的 {@link #membershipProbes} 可以公平比较。
 */
public class NaiveEvaluator {

    protected final InvertedIndex index;
    protected long postReads = 0;
    protected long membershipProbes = 0;
    protected final List<String> trace = new ArrayList<>();
    protected int depth = 0;

    public NaiveEvaluator(InvertedIndex index) {
        this.index = index;
    }

    public EvalResult evaluate(QueryNode node) {
        TreeSet<Integer> result = eval(node);
        return new EvalResult(result, postReads, membershipProbes, List.copyOf(trace));
    }

    protected TreeSet<Integer> eval(QueryNode node) {
        return switch (node) {
            case QueryNode.Term t -> evalTerm(t);
            case QueryNode.Not n -> evalNot(n);
            case QueryNode.And a -> evalAnd(a);
            case QueryNode.Or o -> evalOr(o);
        };
    }

    protected TreeSet<Integer> evalTerm(QueryNode.Term t) {
        TreeSet<Integer> docs = index.postings(t.term());
        postReads += docs.size();
        log("TERM " + t.term() + " -> " + docs.size() + " 篇 " + docs);
        return docs;
    }

    protected TreeSet<Integer> evalNot(QueryNode.Not n) {
        TreeSet<Integer> universe = index.liveDocIds();
        TreeSet<Integer> child = eval(n.child());
        // 差集：遍历全集逐个探测子结果
        TreeSet<Integer> result = new TreeSet<>();
        for (Integer id : universe) {
            membershipProbes++;
            if (!child.contains(id)) {
                result.add(id);
            }
        }
        log("NOT (固定全集 " + universe.size() + " 篇) 减去子结果 " + child.size()
                + " 篇 -> " + result.size() + " 篇 " + result);
        return result;
    }

    protected TreeSet<Integer> evalAnd(QueryNode.And a) {
        log("AND 朴素顺序:");
        depth++;
        TreeSet<Integer> acc = eval(a.children().get(0));
        for (int i = 1; i < a.children().size(); i++) {
            TreeSet<Integer> right = eval(a.children().get(i));
            long before = membershipProbes;
            TreeSet<Integer> intersected = intersect(acc, right);
            log("交集: " + acc.size() + " ∩ " + right.size() + "，探测 "
                    + (membershipProbes - before) + " 次 -> " + intersected.size() + " 篇");
            acc = intersected;
        }
        depth--;
        return acc;
    }

    protected TreeSet<Integer> evalOr(QueryNode.Or o) {
        log("OR 朴素顺序:");
        depth++;
        TreeSet<Integer> acc = eval(o.children().get(0));
        for (int i = 1; i < o.children().size(); i++) {
            TreeSet<Integer> right = eval(o.children().get(i));
            TreeSet<Integer> merged = union(acc, right);
            log("并集: " + acc.size() + " ∪ " + right.size()
                    + " -> " + merged.size() + " 篇");
            acc = merged;
        }
        depth--;
        return acc;
    }

    /** 遍历累加器 acc，对 right 做成员探测；探测次数即 acc 当前大小。 */
    protected final TreeSet<Integer> intersect(TreeSet<Integer> acc, TreeSet<Integer> right) {
        TreeSet<Integer> result = new TreeSet<>();
        for (Integer id : acc) {
            membershipProbes++;
            if (right.contains(id)) {
                result.add(id);
            }
        }
        return result;
    }

    /** 并集直接合并；TreeSet 内部完成去重，不计 contains 探测。 */
    protected final TreeSet<Integer> union(TreeSet<Integer> a, TreeSet<Integer> b) {
        TreeSet<Integer> result = new TreeSet<>(a);
        result.addAll(b);
        return result;
    }

    protected final void log(String message) {
        StringBuilder sb = new StringBuilder();
        sb.append("  ".repeat(Math.max(0, depth)));
        sb.append(message);
        trace.add(sb.toString());
    }
}
