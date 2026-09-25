package cep.service;

import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.pattern.PatternEngine;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/** 内存会话存储（无外部依赖，重启即失）。每个会话保存喂入日志以支持确定性重放。 */
public final class SessionStore {

    /** 一次喂入记录（与引擎的两个 ingest 重载对应）。 */
    public record IngestOp(String id, String key, long timestamp,
                           boolean seqProvided, long seq) {
        void apply(PatternEngine engine) {
            if (seqProvided) {
                engine.ingest(id, key, timestamp, seq);
            } else {
                engine.ingest(id, key, timestamp);
            }
        }
    }

    public static final class Session {
        private final PatternConfig config;
        private final PatternEngine engine;
        private final List<IngestOp> log = new java.util.ArrayList<>();
        private boolean flushed = false;

        Session(PatternConfig config) {
            this.config = config;
            this.engine = new PatternEngine(config);
        }

        public PatternConfig config() { return config; }
        public PatternEngine engine() { return engine; }
        public boolean flushed() { return flushed; }
        void setFlushed(boolean v) { this.flushed = v; }
    }

    private final Map<String, Session> sessions = new ConcurrentHashMap<>();

    public String create(PatternConfig config) {
        String id = UUID.randomUUID().toString();
        sessions.put(id, new Session(config));
        return id;
    }

    public Session require(String id) {
        Session s = sessions.get(id);
        if (s == null) {
            throw ApiException.notFound("会话不存在: " + id);
        }
        return s;
    }

    public void recordAndApply(Session s, IngestOp op) {
        synchronized (s) {
            op.apply(s.engine);
            s.log.add(op);
        }
    }

    /** 重放：reset 后按原始喂入日志重新执行，得到确定性一致的结果。 */
    public EngineResult replayAndFlush(Session s) {
        synchronized (s) {
            s.engine.reset();
            for (IngestOp op : s.log) {
                op.apply(s.engine);
            }
            EngineResult r = s.engine.flush();
            s.flushed = true;
            return r;
        }
    }

    public EngineResult flush(Session s) {
        synchronized (s) {
            EngineResult r = s.engine.flush();
            s.flushed = true;
            return r;
        }
    }

    public EngineResult snapshot(Session s) {
        synchronized (s) {
            return s.engine.snapshot();
        }
    }

    public void advanceWatermark(Session s, long wm) {
        synchronized (s) {
            s.engine.advanceWatermark(wm);
        }
    }

    public int sessionCount() { return sessions.size(); }
}
