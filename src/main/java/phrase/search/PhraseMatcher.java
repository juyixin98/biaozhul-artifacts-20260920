package phrase.search;

import phrase.index.InvertedIndex;
import phrase.index.Occ;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 短语匹配器：在单篇文档内枚举短语查询的全部位置组合。
 *
 * <p>算法：按查询词项顺序 DFS，每层在该词的位置表中用二分查找
 * （{@code lowerBound}）选出相对上一位置落在 {@code [prev+1, prev+slop+1]}
 * 区间内的候选。因为每层候选位置严格递增，
 * <b>重复查询词也不可能两次使用同一位置</b>。
 *
 * <p>输出按位置元组字典序排列（位置表本身有序 + DFS 顺序保证）。
 * 穷举小序列时与 {@link BruteForceReference} 的笛卡尔积过滤结果逐位比较。
 */
public final class PhraseMatcher {

    private final int limit;

    public PhraseMatcher(int limit) {
        if (limit <= 0) {
            throw new IllegalArgumentException("limit must be positive");
        }
        this.limit = limit;
    }

    /**
     * 在指定文档中查找全部命中。
     *
     * @param query 短语查询（field 非空时只统计该字段内的出现）
     * @return 命中列表，按位置元组字典序；超过 limit 时只返回前 limit 个
     */
    public List<Match> find(InvertedIndex idx, String docId, PhraseQuery query) {
        List<String> terms = query.terms();

        List<int[]> perTerm = new ArrayList<>(terms.size());
        for (String term : terms) {
            List<Occ> occs = idx.occurrences(term, docId);
            if (query.isFieldScoped()) {
                List<Integer> inField = new ArrayList<>();
                for (Occ o : occs) {
                    if (o.field().equals(query.field())) {
                        inField.add(o.position());
                    }
                }
                if (inField.isEmpty()) {
                    return List.of();
                }
                int[] arr = new int[inField.size()];
                for (int i = 0; i < arr.length; i++) {
                    arr[i] = inField.get(i);
                }
                perTerm.add(arr);
            } else {
                if (occs.isEmpty()) {
                    return List.of();
                }
                int[] arr = new int[occs.size()];
                for (int i = 0; i < arr.length; i++) {
                    arr[i] = occs.get(i).position();
                }
                perTerm.add(arr);
            }
        }

        List<Match> matches = new ArrayList<>();
        int[] chosen = new int[terms.size()];
        dfs(idx, docId, query.slop(), perTerm, 0, 0, chosen, matches);
        return matches;
    }

    private boolean dfs(InvertedIndex idx, String docId, int slop,
                        List<int[]> perTerm, int depth, int prev, int[] chosen,
                        List<Match> out) {
        if (out.size() >= limit) {
            return false; // 结果已满，停止枚举
        }
        if (depth == perTerm.size()) {
            out.add(buildMatch(idx, docId, chosen));
            return out.size() < limit;
        }
        int[] candidates = perTerm.get(depth);
        int minPos = depth == 0 ? Integer.MIN_VALUE : prev + 1;
        int from = lowerBound(candidates, minPos);

        if (depth == perTerm.size() - 1) {
            // 最后一层：无需关心后继，直接枚举候选
            for (int i = from; i < candidates.length; i++) {
                if (depth > 0 && candidates[i] - prev > slop + 1) {
                    break;
                }
                chosen[depth] = candidates[i];
                if (!dfs(idx, docId, slop, perTerm, depth + 1, candidates[i], chosen, out)) {
                    return false;
                }
            }
            return true;
        }

        for (int i = from; i < candidates.length; i++) {
            if (depth > 0 && candidates[i] - prev > slop + 1) {
                break;
            }
            chosen[depth] = candidates[i];
            // 可行性剪枝：后继词项在 [pos+1, pos+slop+1] 内必须至少有一个位置
            int[] nextCandidates = perTerm.get(depth + 1);
            if (lowerBound(nextCandidates, candidates[i] + 1) >= nextCandidates.length
                    || nextCandidates[lowerBound(nextCandidates, candidates[i] + 1)]
                        > candidates[i] + slop + 1) {
                continue;
            }
            if (!dfs(idx, docId, slop, perTerm, depth + 1, candidates[i], chosen, out)) {
                return false;
            }
        }
        return true;
    }

    private Match buildMatch(InvertedIndex idx, String docId, int[] positions) {
        List<Integer> posList = new ArrayList<>(positions.length);
        List<String> fieldList = new ArrayList<>(positions.length);
        boolean cross = false;
        String firstField = null;
        for (int p : positions) {
            Occ occ = idx.occAt(docId, p);
            String f = occ == null ? "" : occ.field();
            posList.add(p);
            fieldList.add(f);
            if (firstField == null) {
                firstField = f;
            } else if (!f.equals(firstField)) {
                cross = true;
            }
        }
        return new Match(posList, fieldList, positions[0], positions[positions.length - 1], cross);
    }

    /** 第一个 >= target 的下标；全部 < target 时返回 a.length。 */
    static int lowerBound(int[] a, int target) {
        int lo = 0;
        int hi = a.length;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (a[mid] < target) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }
}
