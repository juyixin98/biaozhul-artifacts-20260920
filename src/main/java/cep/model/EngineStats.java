package cep.model;

/** 引擎处理过程的计数统计，便于测试与运维核对。 */
public final class EngineStats {

    public long received;
    public long duplicates;
    public long droppedLate;
    public long acceptedLate;
    public long processed;
    public long matches;
    public long timeouts;
    public long cKilled;
    public long watermarks;

    @Override
    public String toString() {
        return "EngineStats{received=" + received + ", processed=" + processed
                + ", matches=" + matches + ", timeouts=" + timeouts
                + ", cKilled=" + cKilled + ", droppedLate=" + droppedLate
                + ", acceptedLate=" + acceptedLate + ", duplicates=" + duplicates
                + ", watermarks=" + watermarks + "}";
    }
}
