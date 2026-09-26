package com.example.timeout.service;

import java.nio.file.Path;
import java.time.Instant;
import java.util.Objects;

/**
 * 持有当前运行的 {@link TimeoutService}，并支持“原地模拟重启”：
 * 新建时钟（单调读数归零）并仅根据持久化的墙钟截止时间重新换算。
 */
public final class ServiceManager {

    private final Path storeFile;
    private final boolean virtual;
    private final Instant virtualStartWall;
    private volatile TimeoutService current;
    private int generation;

    private ServiceManager(Path storeFile, boolean virtual, Instant virtualStartWall) {
        this.storeFile = storeFile;
        this.virtual = virtual;
        this.virtualStartWall = virtualStartWall;
        this.current = virtual
                ? TimeoutService.virtual(virtualStartWall, storeFile)
                : TimeoutService.system(storeFile);
        this.generation = 1;
    }

    public static ServiceManager virtual(Instant startWall, Path storeFile) {
        return new ServiceManager(storeFile, true, Objects.requireNonNull(startWall));
    }

    public static ServiceManager system(Path storeFile) {
        return new ServiceManager(storeFile, false, null);
    }

    public TimeoutService current() {
        return current;
    }

    public int generation() {
        return generation;
    }

    public boolean isVirtual() {
        return virtual;
    }

    /**
     * 模拟重启：单调钟从新原点起算。虚拟模式可用 {@code wallNow} 指定重启时的墙钟
     * （用于演示停机期间墙钟被 NTP 调整）；为 null 时沿用重启前的墙钟读数。
     * system 模式忽略参数，直接读取真实系统时钟。
     */
    public synchronized TimeoutService restart(Instant wallNow) {
        Instant startWall = virtual
                ? (wallNow != null ? wallNow : current.wallNow())
                : null;
        current.persist();
        current = virtual
                ? TimeoutService.virtual(startWall, storeFile)
                : TimeoutService.system(storeFile);
        generation++;
        return current;
    }
}
