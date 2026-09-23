package com.example.wm.engine;

import com.example.wm.model.Classification;
import com.example.wm.model.LateReason;
import com.example.wm.model.StreamEvent;
import com.example.wm.model.WatermarkConfig;
import com.example.wm.time.VirtualClock;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * 迟到判定边界与乱序宽度 B 的语义测试。
 * 规则：事件时间 t <= 全局水位线 w 即迟到（边界含等于）；t == w+1 准时。
 * 分区水位线 = maxEventTime - B。
 */
class LateAndBoundTest {

    private static StreamEvent ev(String p, long t) {
        return new StreamEvent(p, t, null);
    }

    @Test
    void lateBoundary_isInclusive_equalToWatermarkIsLate() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, Long.MAX_VALUE), clock);

        // 单分区 B=0：事件 100 -> 全局水位线 100
        coord.ingest(ev("p", 100));
        assertEquals(100L, coord.globalWatermark());

        // t == 100 == w：迟到（含等于）
        var equal = coord.ingest(ev("p", 100));
        assertEquals(Classification.LATE, equal.classification());
        assertEquals(LateReason.NORMAL, equal.reason());

        // t == 101 == w+1：准时
        var oneMore = coord.ingest(ev("p", 101));
        assertEquals(Classification.ON_TIME, oneMore.classification());

        // t == 101 == 新水位线 101：再次迟到
        assertEquals(Classification.LATE, coord.ingest(ev("p", 101)).classification());
    }

    @Test
    void outOfOrdernessBound_shiftsLocalWatermarkDown_butLateRuleUnchanged() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(10, Long.MAX_VALUE), clock);
        coord.ingest(ev("p", 100));
        assertEquals(90L, coord.globalWatermark(), "B=10：水位线 = maxEventTime - 10");

        // t=90 == w：迟到；t=91：准时
        assertEquals(Classification.LATE, coord.ingest(ev("p", 90)).classification());
        assertEquals(Classification.ON_TIME, coord.ingest(ev("p", 91)).classification());
        // 91 不推进 max（仍为 100），水位线保持 90
        assertEquals(90L, coord.globalWatermark());

        // 更大的时间戳推进水位线
        coord.ingest(ev("p", 200));
        assertEquals(190L, coord.globalWatermark());
        assertEquals(Classification.LATE, coord.ingest(ev("p", 190)).classification());
    }

    @Test
    void lateDecisionUsesGlobalWatermark_notLocal() {
        VirtualClock clock = new VirtualClock(0);
        var coord = new WatermarkCoordinator(WatermarkConfig.of(0, Long.MAX_VALUE), clock);
        // a 快，b 慢，都从同一水位起步
        coord.ingest(ev("a", 1000));
        clock.advanceBy(1);
        coord.ingest(ev("b", 1000));
        assertEquals(1000L, coord.globalWatermark());

        // b 停滞；a 继续到 3000。全局仍被 b 钉在 1000
        clock.advanceBy(1);
        coord.ingest(ev("a", 2000));
        clock.advanceBy(1);
        coord.ingest(ev("a", 3000));
        assertEquals(1000L, coord.globalWatermark());

        // a 收到一条乱序旧事件 1500：
        //  - 相对 a 自己的本地最大值 3000，它是“旧”的；
        //  - 但相对全局水位线 1000，1500 > 1000 -> 准时。
        // 说明迟到判定锚定的是全局水位线，而不是分区本地进度。
        clock.advanceBy(1);
        var r = coord.ingest(ev("a", 1500));
        assertEquals(Classification.ON_TIME, r.classification(),
                "迟到判定相对全局水位线（1000），即使该事件低于 a 自己的本地最大值（3000）");
        // 乱序事件不推进 a 的 max，全局仍是 1000
        assertEquals(1000L, coord.globalWatermark());

        // 同一事件时间 1500，等 b 把全局顶过 1500 后再到，就是迟到
        clock.advanceBy(1);
        coord.ingest(ev("b", 2000)); // min(a:3000, b:2000)=2000
        clock.advanceBy(1);
        var nowLate = coord.ingest(ev("a", 1500));
        assertEquals(Classification.LATE, nowLate.classification(),
                "全局水位线抬过 1500 后，同样时间戳的事件变为迟到");
    }
}
