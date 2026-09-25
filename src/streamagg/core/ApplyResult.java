package streamagg.core;

/**
 * 一次提交的处置结果。
 *
 * @param status   处置状态
 * @param eventId  事件 ID
 * @param version  规范化后的版本号（去重/滞后时为引擎中记录的版本）
 * @param reason   非 APPLIED 时的原因
 */
public record ApplyResult(Status status, String eventId, long version, String reason) {

    public enum Status {
        /** 操作已应用（可能连带排空了缓存的乱序操作）。 */
        APPLIED,
        /** 乱序版本（存在版本空洞），已缓存等待前置版本到达。 */
        BUFFERED,
        /** 完全重复的滞后操作，幂等忽略。 */
        DUPLICATE,
        /** 同一版本但内容冲突的陈旧操作，拒绝。 */
        STALE_CONFLICT
    }

    static ApplyResult applied(String eventId, long version) {
        return new ApplyResult(Status.APPLIED, eventId, version, null);
    }

    static ApplyResult buffered(String eventId, long version) {
        return new ApplyResult(Status.BUFFERED, eventId, version, "等待更小的版本先到达");
    }

    static ApplyResult duplicate(String eventId, long version) {
        return new ApplyResult(Status.DUPLICATE, eventId, version, "重复操作已幂等忽略");
    }

    static ApplyResult staleConflict(String eventId, long version, String reason) {
        return new ApplyResult(Status.STALE_CONFLICT, eventId, version, reason);
    }
}
