package approxheavy.stream;

import approxheavy.candidates.BoundedCandidates;
import approxheavy.cms.CountMinSketch;

/**
 * Immutable summary of one sealed tumbling window: the sketch (point counts),
 * the bounded candidate set (approximate heavy hitter keys) and the window's
 * half-open time range [startMillis, endMillis).
 */
public final class WindowSummary {
    private final long startMillis;
    private final long endMillis;
    private final CountMinSketch sketch;
    private final BoundedCandidates candidates;
    private final int candidateCapacity;

    public WindowSummary(long startMillis, long endMillis,
                         CountMinSketch sketch, BoundedCandidates candidates,
                         int candidateCapacity) {
        this.startMillis = startMillis;
        this.endMillis = endMillis;
        this.sketch = sketch;
        this.candidates = candidates;
        this.candidateCapacity = candidateCapacity;
    }

    public long startMillis() {
        return startMillis;
    }

    public long endMillis() {
        return endMillis;
    }

    public CountMinSketch sketch() {
        return sketch;
    }

    public BoundedCandidates candidates() {
        return candidates;
    }

    public int candidateCapacity() {
        return candidateCapacity;
    }
}
