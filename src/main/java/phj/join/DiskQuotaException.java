package phj.join;

/** 磁盘溢写额度（字节数）耗尽。 */
public class DiskQuotaException extends RuntimeException {

    private final long limitBytes;
    private final long usedBytes;

    public DiskQuotaException(long limitBytes, long usedBytes, String message) {
        super(message + "（额度 " + limitBytes + " 字节，已用 " + usedBytes + " 字节）");
        this.limitBytes = limitBytes;
        this.usedBytes = usedBytes;
    }

    public long limitBytes() { return limitBytes; }

    public long usedBytes() { return usedBytes; }
}
