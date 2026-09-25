package com.example.phrasesearch.search;

import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 短语搜索器：基于倒排表 + 深度优先枚举。
 *
 * <h3>匹配语义（固定、有序）</h3>
 * 对查询词项序列 t0..t{n-1}，在一篇文档中选择位置 p0..p{n-1}，命中当且仅当：
 * <pre>
 *   p0 &lt; p1 &lt; ... &lt; p{n-1}                       （严格递增）
 *   slopUsed = (p{n-1} - p0) - (n - 1) &lt;= slop    （间隙冗余量受限）
 * </pre>
 * 即允许词项之间插入其他词，但所有“多余间隙”的总数不超过 slop；
 * slop=0 就是经典的相邻精确短语。不允许词项乱序、重叠或复用。
 *
 * <h3>重复词</h3>
 * 严格递增条件保证：即使短语中同一个词出现多次（如 "that that"），两次出现也
 * 必须落在两个不同的位置上，同一位置不可能被两个词项同时占用。
 *
 * <h3>字段范围</h3>
 * q.field() != null：只在该字段的 token 流内匹配（用同字段 Posting 过滤）；
 * q.field() == null：跨字段，在文档全局拼接位置流上匹配，可命中跨越字段边界的短语。
 */
public final class Searcher {

    /** 一篇文档内、一条查询的全部命中（按起始位置有序）。 */
    public List<PhraseMatch> searchDoc(Index index, int docId, PhraseQuery q) {
        int n = q.terms().size();
        List<List<Posting>> perTermCandidates = new ArrayList<>(n);
        for (String term : q.terms()) {
            List<Posting> inDoc = postingsInDoc(index.postings(term), docId, q.field());
            if (inDoc.isEmpty()) {
                return List.of(); // 任一词项在该文档（该字段）缺失，必不命中
            }
            // 倒排表按文档加入/字段插入顺序排列，同一文档内的子序列未必按位置有序，
            // 而 DFS 的二分定位与“后续更大”剪枝都依赖候选按位置升序，这里显式排序。
            List<Posting> sorted = new ArrayList<>(inDoc);
            Collections.sort(sorted);
            perTermCandidates.add(sorted);
        }

        // 单词项短语：每个出现位置都是一次命中，slop 无意义（记为 0）
        if (n == 1) {
            List<PhraseMatch> out = new ArrayList<>();
            for (Posting p : perTermCandidates.get(0)) {
                out.add(new PhraseMatch(docId, index.doc(docId).externalId(),
                        q.terms(), List.of(p), 0, false));
            }
            return out;
        }

        List<PhraseMatch> matches = new ArrayList<>();
        Posting[] chosen = new Posting[n];
        // 初始 prevPos=-1：upperBoundPosition 会从位置 0 开始（传 0 会漏掉首词的位置 0）
        dfs(index, q, docId, perTermCandidates, 0, -1, -1, chosen, matches);
        return matches;
    }

    /** 对索引中全部文档检索，结果按 (docId, 起始位置) 有序。 */
    public List<PhraseMatch> search(Index index, PhraseQuery q) {
        List<PhraseMatch> all = new ArrayList<>();
        for (int docId = 0; docId < index.size(); docId++) {
            all.addAll(searchDoc(index, docId, q));
        }
        return all;
    }

    /**
     * DFS 枚举第 depth 个词项的候选位置。
     *
     * @param firstPos 第 0 个词项已选的位置（depth=0 时暂用 -1）
     */
    private void dfs(Index index, PhraseQuery q, int docId,
                     List<List<Posting>> candidatesPerTerm,
                     int depth, int firstPos, int prevPos,
                     Posting[] chosen, List<PhraseMatch> out) {
        int n = candidatesPerTerm.size();
        if (depth == n) {
            int slopUsed = (chosen[n - 1].position() - chosen[0].position()) - (n - 1);
            boolean crossField = false;
            String f = chosen[0].field();
            for (Posting p : chosen) {
                if (!p.field().equals(f)) {
                    crossField = true;
                    break;
                }
            }
            out.add(new PhraseMatch(docId, index.doc(docId).externalId(),
                    q.terms(), List.of(chosen.clone()), slopUsed, crossField));
            return;
        }

        List<Posting> candidates = candidatesPerTerm.get(depth);
        // 二分找第一个 position > prevPos
        int from = upperBoundPosition(candidates, prevPos);
        int remainingAfter = n - 1 - depth; // 本词之后还要放的词项数
        for (int k = from; k < candidates.size(); k++) {
            Posting cand = candidates.get(k);
            int first = depth == 0 ? cand.position() : firstPos;
            // 假设后续每个词项都只占最小间隙（+1），本词位置为 cand 时最终跨度至少为
            // (cand + remainingAfter) - first；最终 slopUsed 至少为
            // (cand + remainingAfter) - first - (n-1)。
            // 若该下界已超过 slop，后面的候选更大，直接剪枝。
            // 该检查在 depth=0 时恒不触发（保留首词的全部候选位置）。
            int minimalFinalSlop = (cand.position() + remainingAfter) - first - (n - 1);
            if (minimalFinalSlop > q.slop()) {
                break;
            }
            chosen[depth] = cand;
            dfs(index, q, docId, candidatesPerTerm,
                    depth + 1, first, cand.position(), chosen, out);
        }
    }

    /** 在有序 Posting 列表中取 docId 对应（且字段匹配）的连续子列表。 */
    private static List<Posting> postingsInDoc(List<Posting> postings, int docId, String field) {
        int lo = lowerBoundDoc(postings, docId);
        int hi = upperBoundDoc(postings, docId);
        if (lo == hi) {
            return List.of();
        }
        if (field == null) {
            return postings.subList(lo, hi);
        }
        List<Posting> filtered = new ArrayList<>(hi - lo);
        for (int i = lo; i < hi; i++) {
            Posting p = postings.get(i);
            if (p.field().equals(field)) {
                filtered.add(p);
            }
        }
        return filtered;
    }

    private static int lowerBoundDoc(List<Posting> list, int docId) {
        int lo = 0, hi = list.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (list.get(mid).docId() < docId) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    private static int upperBoundDoc(List<Posting> list, int docId) {
        int lo = 0, hi = list.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (list.get(mid).docId() <= docId) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /** 有序列表中第一个 position 严格大于 value 的下标。 */
    private static int upperBoundPosition(List<Posting> list, int value) {
        int lo = 0, hi = list.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (list.get(mid).position() <= value) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /** 工具方法：把扁平命中列表按文档分组（供服务层使用）。 */
    public static List<DocMatches> groupByDoc(List<PhraseMatch> matches) {
        List<DocMatches> groups = new ArrayList<>();
        int lastDocId = -1;
        DocMatches current = null;
        for (PhraseMatch m : matches) {
            if (m.docId() != lastDocId) {
                current = new DocMatches(m.docId(), m.docExternalId(), new ArrayList<>());
                groups.add(current);
                lastDocId = m.docId();
            }
            current.matches().add(m);
        }
        return Collections.unmodifiableList(groups);
    }

    /** 一篇文档内的命中分组。 */
    public record DocMatches(int docId, String docExternalId, List<PhraseMatch> matches) {
    }
}
