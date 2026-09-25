package com.example.tjoin.ref;

import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.StreamEvent;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * Small-data <strong>exact reference implementation</strong> of the interval
 * join: plain O(n&times;m) nested loops with no state, watermarks or
 * buffering. Given the complete sets of left/right events it returns
 * precisely the matching pairs, and is used to cross-check the streaming
 * operator in randomized differential tests.
 *
 * <p>Matching rule: same key and
 * {@code lowerBound <= right.eventTime - left.eventTime <= upperBound}.</p>
 */
public final class ReferenceIntervalJoin {

    /** One exact pair from the reference join. */
    public record Pair(StreamEvent left, StreamEvent right) {

        public long distance() {
            return right.getTimestamp() - left.getTimestamp();
        }
    }

    private ReferenceIntervalJoin() {
    }

    /**
     * Compute every matching (left, right) pair. Duplicate event ids on the
     * same side are treated as distinct events (they are distinct records),
     * matching the streaming semantics.
     */
    public static List<Pair> join(List<StreamEvent> lefts, List<StreamEvent> rights,
                                  JoinConfig config) {
        List<Pair> result = new ArrayList<>();
        for (StreamEvent l : lefts) {
            for (StreamEvent r : rights) {
                if (l.getKey().equals(r.getKey())) {
                    long d = r.getTimestamp() - l.getTimestamp();
                    if (d >= config.lowerBound() && d <= config.upperBound()) {
                        result.add(new Pair(l, r));
                    }
                }
            }
        }
        result.sort(Comparator
                .comparing((Pair p) -> p.left().getTimestamp())
                .thenComparing(p -> p.right().getTimestamp())
                .thenComparing(p -> p.left().getId())
                .thenComparing(p -> p.right().getId()));
        return result;
    }

    /** Same as {@link #join(List, List, JoinConfig)} with explicit bounds. */
    public static List<Pair> join(List<StreamEvent> lefts, List<StreamEvent> rights,
                                  long lowerBound, long upperBound) {
        return join(lefts, rights,
                JoinConfig.symmetric(lowerBound, upperBound, 0, 0, 0, null)
        );
    }
}
