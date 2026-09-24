package phraseindex;

import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Reference oracle for index results: a brute-force per-document scan with
 * no index data. The acceptance suite compares every index query answer
 * against this checker.
 */
public final class BruteForceJudge {

    private final Map<Integer, String> documents;

    public BruteForceJudge(Map<Integer, String> documents) {
        this.documents = documents;
    }

    public Set<Integer> matchingDocIds(Query q) {
        Set<Integer> out = new HashSet<>();
        for (Integer docId : documents.keySet()) {
            if (matches(q, docId, Tokenizer.tokenize(documents.get(docId)))) {
                out.add(docId);
            }
        }
        return out;
    }

    public boolean matches(Query q, int docId, List<String> tokens) {
        if (q instanceof Query.Phrase p) {
            return containsPhrase(tokens, p.terms());
        } else if (q instanceof Query.And a) {
            return matches(a.left(), docId, tokens) && matches(a.right(), docId, tokens);
        } else if (q instanceof Query.Or o) {
            return matches(o.left(), docId, tokens) || matches(o.right(), docId, tokens);
        } else if (q instanceof Query.Not n) {
            return !matches(n.child(), docId, tokens);
        }
        throw new IllegalStateException("unknown query type: " + q.getClass());
    }

    /** Pure sliding-window scan over the token list. */
    public static boolean containsPhrase(List<String> tokens, List<String> terms) {
        if (terms.isEmpty() || tokens.size() < terms.size()) {
            return false;
        }
        outer:
        for (int start = 0; start + terms.size() <= tokens.size(); start++) {
            for (int k = 0; k < terms.size(); k++) {
                if (!tokens.get(start + k).equals(terms.get(k))) {
                    continue outer;
                }
            }
            return true;
        }
        return false;
    }

    public static List<Integer> phrasePositions(List<String> tokens, List<String> terms) {
        java.util.List<Integer> out = new java.util.ArrayList<>();
        if (terms.isEmpty()) {
            return out;
        }
        outer:
        for (int start = 0; start + terms.size() <= tokens.size(); start++) {
            for (int k = 0; k < terms.size(); k++) {
                if (!tokens.get(start + k).equals(terms.get(k))) {
                    continue outer;
                }
            }
            out.add(start);
        }
        return out;
    }
}
