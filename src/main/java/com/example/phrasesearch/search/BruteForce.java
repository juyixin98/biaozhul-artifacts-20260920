package com.example.phrasesearch.search;

import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;

import java.util.ArrayList;
import java.util.List;

/**
 * 穷举参考实现（语义 oracle，刻意写得直白、不做任何优化）：
 * 对每个文档，取每个词项在该文档（该字段）的全部位置，
 * 做完整的多重循环（笛卡尔积），逐一检查严格递增与 slop 条件。
 *
 * 测试用它与 {@link Searcher}（倒排表 + 剪枝 DFS）在小序列上做全枚举比对：
 * 两个实现独立编写、算法不同，结果（含顺序）必须完全一致。
 */
public final class BruteForce {

    public List<PhraseMatch> searchDoc(Index index, int docId, PhraseQuery q) {
        int n = q.terms().size();
        List<List<Posting>> choices = new ArrayList<>(n);
        for (String term : q.terms()) {
            List<Posting> inDoc = new ArrayList<>();
            for (Posting p : index.postings(term)) {
                if (p.docId() == docId && (q.field() == null || p.field().equals(q.field()))) {
                    inDoc.add(p);
                }
            }
            if (inDoc.isEmpty()) {
                return List.of();
            }
            choices.add(inDoc);
        }

        List<PhraseMatch> matches = new ArrayList<>();
        enumerate(index, q, docId, choices, new Posting[n], 0, matches);
        // 枚举顺序本身就是按各词项位置的字典序，与 Searcher 的 DFS 输出顺序一致
        return matches;
    }

    public List<PhraseMatch> search(Index index, PhraseQuery q) {
        List<PhraseMatch> all = new ArrayList<>();
        for (int docId = 0; docId < index.size(); docId++) {
            all.addAll(searchDoc(index, docId, q));
        }
        return all;
    }

    private void enumerate(Index index, PhraseQuery q, int docId,
                           List<List<Posting>> choices, Posting[] picked, int depth,
                           List<PhraseMatch> out) {
        int n = choices.size();
        if (depth == n) {
            // 检查 1：严格递增（重复词因此不可能复用同一位置）
            for (int i = 1; i < n; i++) {
                if (!(picked[i - 1].position() < picked[i].position())) {
                    return;
                }
            }
            // 检查 2：slop（固定、有序）
            int slopUsed = (picked[n - 1].position() - picked[0].position()) - (n - 1);
            if (slopUsed > q.slop()) {
                return;
            }
            boolean crossField = false;
            String f = picked[0].field();
            for (Posting p : picked) {
                if (!p.field().equals(f)) {
                    crossField = true;
                }
            }
            out.add(new PhraseMatch(docId, index.doc(docId).externalId(),
                    q.terms(), List.of(picked.clone()), slopUsed, crossField));
            return;
        }
        for (Posting p : choices.get(depth)) {
            picked[depth] = p;
            enumerate(index, q, docId, choices, picked, depth + 1, out);
        }
    }
}
