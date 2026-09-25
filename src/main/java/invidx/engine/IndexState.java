package invidx.engine;

import invidx.segment.Segment;

import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Immutable state transition value. Every metadata change swaps in a new
 * instance; fields are package-visible for the engine's hot paths.
 * {@code ram} is the single mutable buffer shared across non-flushing
 * transitions.
 */
final class IndexState {

    final List<Segment> segments;
    final Set<Long> deleted;
    final Map<Integer, Long> nextGen;
    final int nextDocId;
    final long segmentSeq;
    final long version;
    final RamBuffer ram;

    IndexState(List<Segment> segments, Set<Long> deleted, Map<Integer, Long> nextGen,
               int nextDocId, long segmentSeq, long version, RamBuffer ram) {
        this.segments = List.copyOf(segments);
        this.deleted = Set.copyOf(deleted);
        this.nextGen = Map.copyOf(nextGen);
        this.nextDocId = nextDocId;
        this.segmentSeq = segmentSeq;
        this.version = version;
        this.ram = ram;
    }

    IndexState withDeletedAndGen(Set<Long> newDeleted, Map<Integer, Long> gens) {
        return new IndexState(segments, newDeleted, gens, nextDocId,
                segmentSeq, version, ram);
    }
}
