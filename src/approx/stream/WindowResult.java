package approx.stream;

import java.util.List;
import java.util.Map;

import approx.cms.SketchSnapshot;

/**
 * Immutable result emitted when a window closes.
 *
 * <p>{@code candidateTopK} is a projection of the bounded candidate set and is
 * not guaranteed to contain every true top-K item. {@code exactTopK} is only
 * populated when the engine was configured with {@code trackExact=true}; it is
 * the ground truth used in tests and acceptance experiments.
 */
public final class WindowResult {

    public final long windowStartMillis;
    public final long windowEndMillis;
    public final long totalCount;
    public final long distinctCandidates;
    public final List<Map.Entry<String, Long>> candidateTopK;
    public final List<Map.Entry<String, Long>> exactTopK; // null unless trackExact
    public final SketchSnapshot sketch;
    public final long errorUpperBound;

    WindowResult(long windowStartMillis, long windowEndMillis, long totalCount, long distinctCandidates,
                 List<Map.Entry<String, Long>> candidateTopK,
                 List<Map.Entry<String, Long>> exactTopK,
                 SketchSnapshot sketch, long errorUpperBound) {
        this.windowStartMillis = windowStartMillis;
        this.windowEndMillis = windowEndMillis;
        this.totalCount = totalCount;
        this.distinctCandidates = distinctCandidates;
        this.candidateTopK = candidateTopK;
        this.exactTopK = exactTopK;
        this.sketch = sketch;
        this.errorUpperBound = errorUpperBound;
    }
}
