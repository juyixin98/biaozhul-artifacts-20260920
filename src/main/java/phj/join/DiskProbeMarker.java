package phj.join;

/** 磁盘位图标记（大探测分区，额度计入 spill store）。 */
public final class DiskProbeMarker implements ProbeMarker {

    private final MarkerFile file;
    private final long n;

    public DiskProbeMarker(SpillStore store, String label, long n) {
        this.file = MarkerFile.create(store, "mark-" + label, n);
        this.n = n;
    }

    @Override
    public void mark(long index) { file.mark(index); }

    @Override
    public boolean isMarked(long index) { return file.isMarked(index); }

    @Override
    public String kind() { return "disk:" + n; }

    @Override
    public void close() { file.close(); }
}
