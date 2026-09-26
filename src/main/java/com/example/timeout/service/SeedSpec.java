package com.example.timeout.service;

import com.example.timeout.rule.DeadlineRule;

/** 启动时灌入的固定测试数据条目。 */
public record SeedSpec(String id, String label, DeadlineRule rule) {
}
