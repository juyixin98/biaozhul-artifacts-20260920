package invidx.search;

import invidx.model.DocKey;
import invidx.segment.Segment;

import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Immutable point-in-time view over the index.
 *
 * <p>A snapshot pairs exactly one published manifest version with the
 * segment set and the durable delete prefix ({@code deleted}), optionally
 * extended with not-yet-flushed RAM state. All three travel together in one
 * immutable object, so every query sees a self-consistent view even while a
 * flush or merge publishes a newer snapshot concurrently.
 */
public record Snapshot(
        long manifestVersion,
        List<Segment> segments,
        Set<Long> deleted,
        Map<Integer, LiveDoc> latest,
        boolean containsRam) {

    /** The currently visible revision of a document id. */
    public record LiveDoc(long gen, String text, String segmentName) {
        public DocKey key(int id) {
            return new DocKey(id, gen);
        }
    }

    public Snapshot {
        segments = List.copyOf(segments);
        deleted = Set.copyOf(deleted);
        latest = Map.copyOf(latest);
    }

    public int docCount() {
        return latest.size();
    }

    public boolean isLive(int id, long gen) {
        return !deleted.contains(DocKey.encode(id, gen));
    }
}
