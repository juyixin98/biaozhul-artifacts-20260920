package com.example.timeout.core;

import java.util.List;

/** 注册表的不可变快照。 */
public record RegistrySnapshot(List<TimeoutEntry> entries) {
    public RegistrySnapshot {
        entries = List.copyOf(entries);
    }
}
