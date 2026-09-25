package streammatch.model;

/**
 * 一次 {@link RemovalReason} 移除的结构化记录。
 *
 * @param key      分区键
 * @param aId      被移除的 A 事件 ID
 * @param reason    移除原因
 * @param cId      若原因为 {@link RemovalReason#INTERRUPTED_BY_C}，记录触发的 C 事件 ID，否则为 null
 */
public record RemovedA(String key, String aId, RemovalReason reason, String cId) {

    public static RemovedA timeout(String key, String aId) {
        return new RemovedA(key, aId, RemovalReason.TIMEOUT, null);
    }

    public static RemovedA interruptedByC(String key, String aId, String cId) {
        return new RemovedA(key, aId, RemovalReason.INTERRUPTED_BY_C, cId);
    }

    public static RemovedA skippedAfterMatch(String key, String aId) {
        return new RemovedA(key, aId, RemovalReason.SKIPPED_AFTER_MATCH, null);
    }
}
