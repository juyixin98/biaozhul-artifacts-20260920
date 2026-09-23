package phj.join;

import java.util.Arrays;

/** 内存位图标记（探测分区行数 <= 内存阈值时使用）。 */
public final class MemoryProbeMarker implements ProbeMarker {

    private final byte[] flags;

    public MemoryProbeMarker(int n) {
        this.flags = new byte[n];
    }

    @Override
    public void mark(long index) {
        flags[(int) index] = 1;
    }

    @Override
    public boolean isMarked(long index) {
        return flags[(int) index] == 1;
    }

    @Override
    public String kind() {
        return "memory:" + flags.length;
    }

    @Override
    public String toString() {
        return "MemoryProbeMarker" + Arrays.toString(flags);
    }
}
