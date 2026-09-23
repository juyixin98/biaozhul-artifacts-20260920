package com.example.quantiles.model;

/**
 * 一个整数取值的事件。
 *
 * @param timestampMillis 事件时间（毫秒），允许为负值（纪元之前）
 * @param value           事件的整数值，允许为负值、允许重复
 */
public record Event(long timestampMillis, long value) {
}
