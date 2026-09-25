package cep.model;

/**
 * 一个 A 在时间窗口内没有等到有效 B（且未被 C 杀掉、未被非重叠策略消耗）。
 *
 * @param deadline 超时时刻（A.timestamp + windowMs），半开：wm > deadline 才超时
 */
public record Timeout(String aId, long aTimestamp, long deadline, boolean late) {

    @Override
    public String toString() {
        return "Timeout[" + aId + "@" + aTimestamp + ", deadline=" + deadline
                + (late ? ", late" : "") + "]";
    }
}
