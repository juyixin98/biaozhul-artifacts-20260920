package cep.model;

import java.util.List;

/** 一次评估/截至当前水位的完整输出。 */
public final class EngineResult {

    private final List<Match> matches;
    private final List<Timeout> timeouts;
    private final EngineStats stats;
    private final long watermark;

    public EngineResult(List<Match> matches, List<Timeout> timeouts,
                        EngineStats stats, long watermark) {
        this.matches = List.copyOf(matches);
        this.timeouts = List.copyOf(timeouts);
        this.stats = stats;
        this.watermark = watermark;
    }

    public List<Match> matches() { return matches; }
    public List<Timeout> timeouts() { return timeouts; }
    public EngineStats stats() { return stats; }
    public long watermark() { return watermark; }
}
