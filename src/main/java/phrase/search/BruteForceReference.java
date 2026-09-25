package phrase.search;

import phrase.index.InvertedIndex;
import phrase.index.Occ;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 穷举参考实现（“蛮力法”）：对每个查询词项取该文档全部候选位置，
 * 做笛卡尔积，逐个过滤出“严格递增且相邻差 &lt;= slop+1”的元组。
 *
 * <p>仅用于小序列验证生产实现 {@link PhraseMatcher} 的正确性
 * （见 brute_force_equivalence 测试），不参与在线检索路径。
 */
public final class BruteForceReference {

    private BruteForceReference() {}

    /** 返回全部合法位置元组（字典序），不设上限。 */
    public static List<int[]> enumerate(InvertedIndex idx, String docId, PhraseQuery query) {
        List<int[]> perTerm = new ArrayList<>();
        for (String term : query.terms()) {
            List<Occ> occs = idx.occurrences(term, docId);
            int[] arr;
            if (query.isFieldScoped()) {
                arr = occs.stream()
                        .filter(o -> o.field().equals(query.field()))
                        .mapToInt(Occ::position)
                        .toArray();
            } else {
                arr = occs.stream().mapToInt(Occ::position).toArray();
            }
            if (arr.length == 0) {
                return List.of();
            }
            perTerm.add(arr);
        }

        List<int[]> valid = new ArrayList<>();
        int n = perTerm.size();
        int[] idxPos = new int[n];
        int total = 1;
        for (int[] a : perTerm) {
            total *= a.length;
        }
        for (long t = 0; t < total; t++) {
            long code = t;
            for (int i = 0; i < n; i++) {
                int[] a = perTerm.get(i);
                idxPos[i] = (int) (code % a.length);
                code /= a.length;
            }
            int[] tuple = new int[n];
            for (int i = 0; i < n; i++) {
                tuple[i] = perTerm.get(i)[idxPos[i]];
            }
            if (isValid(tuple, query.slop())) {
                valid.add(tuple);
            }
        }
        valid.sort((x, y) -> Arrays.compare(x, y));
        return valid;
    }

    static boolean isValid(int[] p, int slop) {
        for (int i = 1; i < p.length; i++) {
            int gap = p[i] - p[i - 1];
            if (gap < 1 || gap > slop + 1) {
                return false;
            }
        }
        return true;
    }
}
