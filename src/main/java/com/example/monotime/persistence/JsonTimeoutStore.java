package com.example.monotime.persistence;

import com.example.monotime.domain.ScheduledTimeout;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/**
 * 基于本地 JSON 文件的持久化（每条超时一个 {@code <id>.json}）。
 *
 * <p>纯本地、无数据库；{@link ScheduledTimeout} 落盘时显式投影为
 * {@link PersistedTimeout}，从结构上杜绝单调刻度被持久化。</p>
 *
 * <p>健壮性：id 在系统边界严格校验（拒绝而非有损替换，避免不同 id 碰撞到同一文件）；
 * 写入走“临时文件 + 原子 move”，避免崩溃留下半截 JSON；
 * {@link #findAll()} 跳过单个损坏文件而不是让整目录恢复失败。</p>
 */
public final class JsonTimeoutStore implements TimeoutStore {

    private static final String SUFFIX = ".json";
    private static final Pattern SAFE_ID = Pattern.compile("[A-Za-z0-9._-]{1,128}");

    private final Path directory;
    private final ObjectMapper mapper;

    public JsonTimeoutStore(Path directory, ObjectMapper mapper) {
        this.directory = directory;
        this.mapper = mapper;
    }

    public void initialize() throws IOException {
        Files.createDirectories(directory);
    }

    public Path directory() {
        return directory;
    }

    @Override
    public void save(ScheduledTimeout timeout) throws IOException {
        initialize();
        PersistedTimeout record = new PersistedTimeout(
                timeout.timeoutId(),
                timeout.ruleId(),
                timeout.ruleVersion(),
                timeout.scheduledAtInstant(),
                timeout.deadlineInstant(),
                timeout.tzdbVersion());
        Path target = pathFor(timeout.timeoutId());
        Path tmp = directory.resolve("." + target.getFileName() + ".tmp-" + System.nanoTime());
        // 先写同目录临时文件，再原子替换：崩溃也不会留下截断的正式记录。
        Files.writeString(tmp, mapper.writerWithDefaultPrettyPrinter().writeValueAsString(record));
        try {
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException atomicNotSupported) {
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    @Override
    public Optional<PersistedTimeout> find(String timeoutId) throws IOException {
        Path file = pathFor(timeoutId);
        if (!Files.exists(file)) {
            return Optional.empty();
        }
        return Optional.of(read(file));
    }

    @Override
    public List<PersistedTimeout> findAll() throws IOException {
        if (!Files.isDirectory(directory)) {
            return List.of();
        }
        List<PersistedTimeout> records = new ArrayList<>();
        try (Stream<Path> stream = Files.list(directory)) {
            List<Path> files = stream
                    .filter(p -> p.getFileName().toString().endsWith(SUFFIX))
                    .sorted(Comparator.comparing(p -> p.getFileName().toString()))
                    .toList();
            for (Path file : files) {
                try {
                    records.add(read(file));
                } catch (IOException | UncheckedIOException corrupt) {
                    // 单条损坏不应拖垮整目录恢复：跳过并给出可定位的文件名。
                    System.err.println("警告：跳过无法解析的持久化文件 " + file.getFileName()
                            + "（" + corrupt.getMessage() + "）");
                }
            }
        }
        return List.copyOf(records);
    }

    @Override
    public void delete(String timeoutId) throws IOException {
        Files.deleteIfExists(pathFor(timeoutId));
    }

    private PersistedTimeout read(Path file) throws IOException {
        return mapper.readValue(Files.readString(file), PersistedTimeout.class);
    }

    private Path pathFor(String timeoutId) {
        return directory.resolve(validateId(timeoutId) + SUFFIX);
    }

    /**
     * 严格校验 id：只允许安全字符且长度受限，拒绝路径分隔符、空段与 {@code .}/{@code ..}。
     * 拒绝而非替换，避免 {@code a/b} 与 {@code a_b} 这类不同 id 映射到同一文件互相覆盖。
     */
    static String validateId(String timeoutId) {
        if (timeoutId == null || !SAFE_ID.matcher(timeoutId).matches()) {
            throw new IllegalArgumentException(
                    "非法 timeoutId（仅允许 1-128 位字母数字、点、下划线、连字符）: " + timeoutId);
        }
        if (timeoutId.equals(".") || timeoutId.equals("..")) {
            throw new IllegalArgumentException("非法 timeoutId: " + timeoutId);
        }
        return timeoutId;
    }
}
