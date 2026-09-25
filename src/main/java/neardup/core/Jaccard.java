package neardup.core;

import java.util.Set;

/** Exact Jaccard similarity over two finite sets: |A∩B| / |A∪B|. */
public final class Jaccard {

    private Jaccard() {
    }

    /**
     * Exact Jaccard similarity. Two empty sets are NOT considered similar
     * (returns 0.0): empty-shingle documents must not match one another just
     * because both are empty.
     */
    public static double similarity(Set<?> a, Set<?> b) {
        if (a.isEmpty() && b.isEmpty()) {
            return 0.0;
        }
        int inter = 0;
        Set<?> small = a.size() <= b.size() ? a : b;
        Set<?> large = small == a ? b : a;
        for (Object o : small) {
            if (large.contains(o)) {
                inter++;
            }
        }
        int union = a.size() + b.size() - inter;
        if (union == 0) {
            return 0.0;
        }
        return (double) inter / (double) union;
    }
}
