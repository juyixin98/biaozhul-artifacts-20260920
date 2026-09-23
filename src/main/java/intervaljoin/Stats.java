package intervaljoin;

/** 引擎运行指标。purge 计数即题目要求输出的“被回收状态量”。 */
public final class Stats {
    public long leftAccepted;
    public long rightAccepted;
    public long leftLateDropped;
    public long rightLateDropped;
    public long pairsEmitted;
    /** 被回收（从状态中删除）的左事件数。 */
    public long leftPurged;
    /** 被回收（从状态中删除）的右事件数。 */
    public long rightPurged;
    /** 被整体清空的 key 状态条数（左右分开计）。 */
    public long leftKeySlotsRemoved;
    public long rightKeySlotsRemoved;
}
