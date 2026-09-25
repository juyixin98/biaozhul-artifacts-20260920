package streammatch.engine;

import streammatch.model.EngineConfig;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.Match;

import java.util.List;

/**
 * 流式模式匹配器：增量接收事件，维护等待状态并产出匹配。
 *
 * <h2>顺序语义（全序定义）</h2>
 * 事件按“到达顺序”被赋予单调递增的 {@code seq}。引擎中所有先后判断都基于全序：
 * <ul>
 *   <li>事件时间模式：{@code (timestamp, seq)}，时间戳相同时先到达者在前；</li>
 *   <li>处理时间模式：{@code (orderTime, seq)}，orderTime 为事件到达瞬间注入时钟的读数，
 *       同一毫秒内先到达者在前。</li>
 * </ul>
 *
 * <h2>模式判定（A 后 B 且期间无 C）</h2>
 * 对等待中的 A 与到达的 B（同 key）：{@code 0 <= tB - tA <= windowMillis} 且
 * 在该 key 的全序上、开区间 (A, B) 内不存在 C。
 * <ul>
 *   <li>tA == tB：合法（同时刻按 seq 区分先后，seqA &lt; seqB 即可匹配）；</li>
 *   <li>C 与某事件同刻：由全序唯一决定归属——C 打断所有“在全序上位于 C 之前”
 *       （{@code tA < tC}，或 {@code tA == tC && seqA < seqC}）的等待中 A。
 *       由于等待中的 A 必然更早到达（seq 更小），对同刻 A 而言 C 排在其后即打断之；
 *       因此 (A, C, B) 三者同刻、按该到达顺序喂入时，A 被 C 打断，与 B 不匹配。</li>
 * </ul>
 *
 * <h2>超时与迟到</h2>
 * <ul>
 *   <li>事件时间：watermark = 已见最大事件时间 - allowedLateness。A 在
 *       {@code watermark > tA + windowMillis} 时超时清理；timestamp &lt; watermark 的
 *       事件为迟到事件，按 {@link streammatch.model.LatePolicy} 处理。</li>
 *   <li>处理时间：A 在到达后 windowMillis 的闭窗口结束时（tA+W+1 逻辑毫秒）由
 *       调度器定时器清理。</li>
 * </ul>
 */
public interface PatternMatcher {

    /** 当前配置（窗口等部分字段可运行期修改，见实现类）。 */
    EngineConfig config();

    /**
     * 按列表顺序处理一批事件（即一次请求的到达顺序）。返回这批事件产生的增量结果。
     * 事件的 {@code seq} 字段由引擎按到达顺序重新分配，调用方无需填写。
     */
    EngineResult process(List<Event> arrivals);

    /** 单事件便捷入口。 */
    default EngineResult processOne(Event e) {
        return process(List.of(e));
    }

    /**
     * 事件时间模式：把 watermark 推进到 {@code targetWatermark}（取历史最大值），
     * 随之清理超时 A。处理时间模式不支持（抛 {@link UnsupportedOperationException}）。
     * 常用于无新事件时强制收尾超时。
     */
    EngineResult advanceWatermark(long targetWatermark);

    /** 截至目前累计发出的全部匹配（按发出顺序）。 */
    List<Match> matches();

    /** 当前仍在等待 B 的 A 事件 ID（快照，按 key、时间、seq 排序）。 */
    List<String> activeAIds();

    /** 当前 watermark（处理时间模式返回 {@link Long#MIN_VALUE}）。 */
    long watermark();
}
