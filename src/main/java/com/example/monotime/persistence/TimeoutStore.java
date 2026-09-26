package com.example.monotime.persistence;

import com.example.monotime.domain.ScheduledTimeout;

import java.io.IOException;
import java.util.List;
import java.util.Optional;

/**
 * 超时记录持久化。
 *
 * <p><b>关键约束</b>：持久化内容只含墙钟域信息（截止瞬间、安排瞬间、规则、TZDB 版本），
 * <b>绝不保存单调刻度</b>——{@code nanoTime} 纪元随 JVM 死亡而失效，存下来必然被误用。
 * 重启后由转换服务用墙钟截止瞬间重新锚定（重新计算单调死线）。</p>
 */
public interface TimeoutStore {

    void save(ScheduledTimeout timeout) throws IOException;

    Optional<PersistedTimeout> find(String timeoutId) throws IOException;

    List<PersistedTimeout> findAll() throws IOException;

    void delete(String timeoutId) throws IOException;
}
