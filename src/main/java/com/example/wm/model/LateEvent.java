package com.example.wm.model;

/**
 * 进入迟到通道的事件。
 *
 * @param event                  原始事件
 * @param reason                 迟到原因（普通 / 恢复重放）
 * @param globalWatermarkMs      判定时的全局水位线（无则为 null）
 * @param detectedAtProcessingMs 判定时的处理时间（注入时钟读数）
 */
public record LateEvent(
        StreamEvent event,
        LateReason reason,
        Long globalWatermarkMs,
        long detectedAtProcessingMs
) {
}
