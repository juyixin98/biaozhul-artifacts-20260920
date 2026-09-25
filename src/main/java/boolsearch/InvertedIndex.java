package boolsearch;

import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * 倒排索引：term -> 有序文档 id 集合。
 * 维护“固定文档全集”（当前存活文档集合），NOT 语义相对该全集求补。
 * 支持删除文档：从全集与所有倒排表中移除。
 */
public final class InvertedIndex {
    private final Map<String, TreeSet<Integer>> postings = new HashMap<>();
    private final Map<Integer, Set<String>> docTerms = new HashMap<>();
    private final TreeSet<Integer> universe = new TreeSet<>();

    /** 新增或覆盖一篇文档。 */
    public synchronized void addDocument(int id, String text) {
        deleteDocument(id);
        Set<String> terms = new TreeSet<>(Tokenizer.tokenize(text));
        docTerms.put(id, terms);
        universe.add(id);
        for (String t : terms) {
            postings.computeIfAbsent(t, k -> new TreeSet<>()).add(id);
        }
    }

    /** 删除文档；不存在返回 false。 */
    public synchronized boolean deleteDocument(int id) {
        Set<String> terms = docTerms.remove(id);
        if (terms == null) return false;
        universe.remove(id);
        for (String t : terms) {
            TreeSet<Integer> p = postings.get(t);
            if (p != null) {
                p.remove(id);
                if (p.isEmpty()) postings.remove(t);
            }
        }
        return true;
    }

    /** 某词项的倒排表（有序、只读快照语义由调用方保证不修改）。未知词返回空集。 */
    public synchronized SortedSet<Integer> posting(String term) {
        TreeSet<Integer> p = postings.get(term);
        if (p == null) return new TreeSet<>();
        return Collections.unmodifiableSortedSet(p);
    }

    /** 固定文档全集：当前所有存活文档 id（升序）。 */
    public synchronized SortedSet<Integer> universe() {
        return Collections.unmodifiableSortedSet(universe);
    }

    public synchronized int docCount() {
        return universe.size();
    }

    /** 某词项的文档频率（倒排表大小），未知词为 0。 */
    public synchronized int df(String term) {
        TreeSet<Integer> p = postings.get(term);
        return p == null ? 0 : p.size();
    }
}
