package dedup.watermark;

import dedup.core.Event;

/**
 * 纯手动水位线：既不从事件推导，也不被周期时钟推进，只由外部 API 显式设置。
 *
 * <p>适合“事件时间与注入调度完全解耦”的测试与演示：去重行为不依赖墙钟，
 * 即使处理机器时钟发生回退，只要外部按序推进水位线，结果就完全确定。
 */
public final class ManualWatermarkGenerator implements WatermarkGenerator {

    private Long watermark;

    @Override
    public Long onEvent(Event event) {
        return null; // 事件不推进水位线
    }

    @Override
    public Long onPeriodicTick(long currentClockMillis) {
        return null; // 周期触发也不推进
    }

    @Override
    public Long currentWatermark() {
        return watermark;
    }

    /**
     * 显式设置水位线。
     *
     * @return true 表示水位线前进；回退（时钟回退语义）返回 false 且不更新
     */
    public boolean setWatermark(long newWatermark) {
        if (watermark != null && newWatermark < watermark) {
            return false;
        }
        if (watermark != null && newWatermark == watermark) {
            return false;
        }
        watermark = newWatermark;
        return true;
    }

    /** 恢复用：直接装载（不做单调性检查）。 */
    public void restore(long watermark) {
        this.watermark = watermark;
    }

    /** 重置为“从未推进”状态（admin reset 使用）。 */
    public void reset() {
        this.watermark = null;
    }
}
