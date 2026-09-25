package boolsearch.eval;

import boolsearch.InvertedIndex;
import boolsearch.query.Node;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * 布尔查询求值器。
 *
 * 语义：
 *  - Term -> 倒排表
 *  - AND  -> 交集；OR -> 并集
 *  - NOT  -> 相对“固定文档全集”（索引当前存活文档集合）求补
 *
 * optimize=false：AND 子节点按书写顺序逐个求交（朴素计划）。
 * optimize=true ：AND 子节点先全部求值，再按结果集大小升序求交
 *                 （小表驱动大表，经典交集顺序优化），中间结果为空时短路。
 *
 * 两种计划仅求值顺序不同，结果集必须一致（由测试穷举验证）。
 */
public final class Evaluator {
    private final InvertedIndex index;
    private final boolean optimize;
    private List<String> trace;

    public Evaluator(InvertedIndex index, boolean optimize) {
        this.index = index;
        this.optimize = optimize;
    }

    /** 可选：传入列表以记录 AND 节点实际求交顺序（各子结果集大小）。 */
    public void setTrace(List<String> trace) {
        this.trace = trace;
    }

    public SortedSet<Integer> eval(Node node) {
        if (node instanceof Node.Term t) {
            return new TreeSet<>(index.posting(t.term()));
        }
        if (node instanceof Node.Not n) {
            TreeSet<Integer> r = new TreeSet<>(index.universe());
            r.removeAll(eval(n.child()));
            return r;
        }
        if (node instanceof Node.And a) {
            List<SortedSet<Integer>> sets = new ArrayList<>();
            for (Node c : a.children()) sets.add(eval(c));
            if (optimize) {
                sets.sort(Comparator.comparingInt(SortedSet::size));
            }
            if (trace != null) {
                List<Integer> sizes = new ArrayList<>();
                for (SortedSet<Integer> s : sets) sizes.add(s.size());
                trace.add("AND intersect order sizes=" + sizes + (optimize ? " (optimized)" : " (naive)"));
            }
            TreeSet<Integer> acc = new TreeSet<>(sets.get(0));
            for (int i = 1; i < sets.size(); i++) {
                acc.retainAll(sets.get(i));
                if (optimize && acc.isEmpty()) break; // 短路：已为空无需再交
            }
            return acc;
        }
        if (node instanceof Node.Or o) {
            TreeSet<Integer> acc = new TreeSet<>();
            for (Node c : o.children()) acc.addAll(eval(c));
            return acc;
        }
        throw new IllegalStateException("未知节点: " + node);
    }
}
