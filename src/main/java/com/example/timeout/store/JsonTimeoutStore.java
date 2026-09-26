package com.example.timeout.store;

import com.example.timeout.core.RegistrySnapshot;
import com.example.timeout.core.TimeoutEntry;
import com.example.timeout.core.TimeoutStatus;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.List;

/**
 * 基于本地 JSON 文件的固定存储（演示/测试用）。
 *
 * <p><b>关键约束：</b>落盘的 {@link StoredTimeout} 只包含墙钟截止时间与业务字段，
 * 单调触发读数（fireMonoNanos 等）是进程内瞬时状态，序列化时在类型层面就无法写出。
 * 重启后 {@link #load()} 出来的条目单调字段为占位值，必须交给
 * {@code TimeoutRegistry.recover} 重新换算。
 */
public final class JsonTimeoutStore implements TimeoutStore {

    /** 持久化记录：刻意不含任何单调时钟字段。 */
    public record StoredTimeout(String id, String label, Instant deadlineWall,
                                TimeoutStatus status, String ruleSource) {

        static StoredTimeout from(TimeoutEntry e) {
            return new StoredTimeout(e.id(), e.label(), e.deadlineWall(), e.status(), e.ruleSource());
        }

        TimeoutEntry toEntry() {
            return new TimeoutEntry(id, label, deadlineWall, null, 0L, 0L, 0L,
                    status == null ? TimeoutStatus.SCHEDULED : status, null, null, ruleSource);
        }
    }

    private final Path file;
    private final ObjectMapper mapper;

    public JsonTimeoutStore(Path file) {
        this.file = file;
        this.mapper = JsonMappers.mapper();
    }

    @Override
    public void persist(RegistrySnapshot snapshot) {
        List<StoredTimeout> stored = snapshot.entries().stream().map(StoredTimeout::from).toList();
        Path tmp = file.resolveSibling(file.getFileName() + ".tmp");
        try {
            if (file.getParent() != null) {
                Files.createDirectories(file.getParent());
            }
            mapper.writerWithDefaultPrettyPrinter().writeValue(tmp.toFile(), stored);
            Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            throw new StoreException("failed to persist timeouts to " + file, e);
        }
    }

    @Override
    public List<TimeoutEntry> load() {
        if (!Files.exists(file)) {
            return List.of();
        }
        try {
            StoredTimeout[] stored = mapper.readValue(file.toFile(), StoredTimeout[].class);
            return java.util.Arrays.stream(stored).map(StoredTimeout::toEntry).toList();
        } catch (IOException e) {
            throw new StoreException("failed to load timeouts from " + file, e);
        }
    }
}
