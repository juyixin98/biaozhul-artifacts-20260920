package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import com.example.quantiles.quantile.Fraction;
import com.example.quantiles.quantile.TreeMapAccumulator;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 事件时间滑动窗口精确分位数算子（pane 分片实现）。
 *
 * <h3>窗口定义</h3>
 * <ul>
 *   <li>窗口半开：{@code [windowStart, windowStart + sizeMillis)}；</li>
 *   <li>窗口起点对齐到 slide 网格，事件按事件时间（而非到达顺序）归属；</li>
 *   <li>一个窗口被切成长度 {@code slideMillis} 的若干 pane；
 *       一个事件只写入它所在的 <b>一个</b> pane，窗口触发时合并其全部 pane。</li>
 * </ul>
 *
 * <h3>触发与迟到</h3>
 * <ul>
 *   <li>watermark 单调不减；窗口 {@code [start,end)} 在
 *       {@code watermark >= end + allowedLateness} 时触发一次；</li>
 *   <li>触发后，起点不晚于该窗口起点的 pane 不可能再属于任何<em>未触发</em>窗口，
 *       统一删除（{@code headMap}）——<b>过期删除同步维护精确计数</b>
 *       （各 pane 独立累积器，删除即丢弃整个累积器）；</li>
 *   <li>事件到达时若它能落入的最晚窗口都已触发（pane 起点过早），计为迟到丢弃。</li>
 * </ul>
 *
 * 该类为有状态组件，非线程安全；事件推进与 watermark 推进应由同一驱动线程调用
 * （测试中用手动调度器，生产中由 {@code StreamingQuantileJob} 串行化）。
 */
public final class SlidingWindowQuantileOperator {

    private final WindowSpec spec;

    /** pane 起点 -> 该 pane 的精确多重集（TreeMap 计数，非草图）。 */
    private final TreeMap<Long, TreeMapAccumulator> panes = new TreeMap<>();

    /** 已确认的 watermark（Long.MIN_VALUE 表示尚未推进）。 */
    private long currentWatermark = Long.MIN_VALUE;

    /** 下一个待触发窗口的起点；null 表示还没有任何 pane 建立网格基准。 */
    private Long nextWindowStart;

    /** 曾经见过的最右 pane 起点（决定 flush 时窗口序列的终止位置）。 */
    private long maxPaneStartEverSeen = Long.MIN_VALUE;

    private long lateDroppedCount;
    private long totalReceivedCount;

    public SlidingWindowQuantileOperator(WindowSpec spec) {
        this.spec = spec;
    }

    /**
     * 处理一个事件。返回 true 表示事件被某个未触发窗口接收；
     * false 表示事件迟到（它所属窗口均已触发），已计入 {@link #lateDroppedCount()}。
     */
    public boolean process(Event event) {
        totalReceivedCount++;
        long paneStart = spec.paneStartForTimestamp(event.timestampMillis());
        // 含该 pane 的窗口起点集合为 {paneStart-(k-1)*s, ..., paneStart}，k=panesPerWindow。
        // 事件能落入的最晚窗口起点 = paneStart 自身。
        long latestWindowForPane = paneStart;

        if (nextWindowStart != null && latestWindowForPane + spec.sizeMillis()
                + spec.allowedLatenessMillis() <= currentWatermark) {
            // 该事件能落入的最晚窗口都已经触发 -> 彻底迟到
            lateDroppedCount++;
            return false;
        }

        panes.computeIfAbsent(paneStart, k -> new TreeMapAccumulator()).add(event.value());
        maxPaneStartEverSeen = Math.max(maxPaneStartEverSeen, paneStart);
        if (nextWindowStart == null) {
            // 输出窗口序列从“首个 pane 能落入的最早窗口”开始（网格对齐）。
            nextWindowStart = paneStart - (spec.panesPerWindow() - 1) * spec.slideMillis();
        }
        return true;
    }

    /**
     * 推进 watermark，返回所有新触发窗口的结果（按窗口起点升序）。
     * watermark 不允许倒退。
     *
     * <p>窗口序列在“最后一个可能含有事件的窗口”触发后终止：一个窗口要可能非空，
     * 其覆盖范围必须与现存 pane 有交集；其后的网格窗口不再产出（即便 watermark
     * 推到 {@code Long.MAX_VALUE} 也不会无限生成空窗口）。
     */
    public List<WindowResult> advanceWatermark(long newWatermark) {
        if (newWatermark < currentWatermark) {
            throw new IllegalArgumentException(
                    "watermark 不允许倒退: current=" + currentWatermark + " new=" + newWatermark);
        }
        currentWatermark = newWatermark;
        List<WindowResult> results = new ArrayList<>();
        if (nextWindowStart == null) {
            return results;
        }
        long ppw = spec.panesPerWindow();
        long globalLastPaneStart = maxPaneStartEverSeen;
        while (true) {
            long start = nextWindowStart;
            long end = start + spec.sizeMillis();
            if (currentWatermark < end + spec.allowedLatenessMillis()) {
                break; // 尚未到期
            }
            // 已到期。窗口起点若已经超过“曾经出现过的最右 pane 起点”，
            // 则该窗口及其后的所有窗口都不可能再含任何事件 -> 终止
            //（窗口覆盖的首个 pane 起点就是 start；start > 最右 pane 时再无数据可落入）。
            if (start > globalLastPaneStart) {
                break;
            }
            results.add(emitWindow(start, end, ppw));
            // 清理：起点 <= 刚触发窗口起点的 pane，此后不再属于任何<em>未触发</em>窗口
            //（下一个待触发窗口起点为 start+slide，它的最老 pane 起点也是 start+slide）。
            panes.headMap(start + 1L).clear();
            nextWindowStart = start + spec.slideMillis();
        }
        return results;
    }

    private WindowResult emitWindow(long start, long end, long panesPerWindow) {
        TreeMapAccumulator merged = new TreeMapAccumulator();
        long firstPaneStart = start;
        long lastPaneStart = start + (panesPerWindow - 1) * spec.slideMillis();
        // 合并该窗口覆盖的全部 pane（缺失的 pane 视为空——空窗口的来源之一）
        for (Map.Entry<Long, TreeMapAccumulator> e :
                panes.subMap(firstPaneStart, true, lastPaneStart, true).entrySet()) {
            merged.addAll(e.getValue());
        }
        long n = merged.count();
        List<Fraction> values;
        if (n == 0) {
            values = new ArrayList<>(spec.quantiles().size());
            for (int i = 0; i < spec.quantiles().size(); i++) {
                values.add(null);
            }
        } else {
            values = merged.quantiles(spec.quantiles());
        }
        return new WindowResult(start, end, n, values);
    }

    public long currentWatermark() {
        return currentWatermark;
    }

    public long lateDroppedCount() {
        return lateDroppedCount;
    }

    public long totalReceivedCount() {
        return totalReceivedCount;
    }

    /** 当前仍保留的 pane 数（测试用于验证过期清理）。 */
    public int retainedPanes() {
        return panes.size();
    }

    /** 当前保留 pane 中事件总数（测试用于验证过期删除维护计数）。 */
    public long retainedEventCount() {
        long sum = 0;
        for (TreeMapAccumulator a : panes.values()) {
            sum += a.count();
        }
        return sum;
    }
}
