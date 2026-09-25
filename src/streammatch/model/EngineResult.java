package streammatch.model;

import java.util.List;

/**
 * 引擎处理一批（或一个）事件后的增量结果。
 *
 * @param matches         本次处理新发出的匹配（按发出顺序；含所用事件 ID）
 * @param removed         本次被清理的等待中 A（超时 / 被 C 打断 / 策略跳过）
 * @param lateDropped     本次因迟到被 DROP 策略丢弃的事件 ID（REJECT 不会出现——请求会整体失败）
 * @param watermarkMillis 处理后的 watermark（仅事件时间模式；处理时间模式恒为
 *                        {@link Long#MIN_VALUE} 表示不适用）
 * @param activeAKeys     处理后仍在等待 B 的 A 事件 ID 列表（按 key、时间、seq 排序，便于观察状态）
 */
public record EngineResult(List<Match> matches,
                           List<RemovedA> removed,
                           List<String> lateDropped,
                           long watermarkMillis,
                           List<String> activeAKeys) {

    public static EngineResult empty(long watermark, List<String> active) {
        return new EngineResult(List.of(), List.of(), List.of(), watermark, active);
    }
}
