package com.example.txflow;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一致性快照。
 *
 * 一个快照文件同时记录三样必须互相对齐的东西：
 *   1) lastCommittedOffset：已提交到接收器的最后一条输入记录偏移；
 *   2) committedCount：已提交事务数（用于和接收器 committed.log 核对）；
 *   3) prepared：已“准备但尚未提交”的事务（两阶段提交的中间态），
 *      含该事务生效后的完整算子状态 stateAfter；
 *   4) stateCommitted：已提交状态（恢复基线）。
 *
 * 文件通过 FileIO.writeAtomically 整体落盘（tmp + fsync + rename + 目录 fsync），
 * 因此任何崩溃后读到的快照要么是上一版完整内容、要么是这一版完整内容，
 * 不存在撕裂。
 */
public class Checkpoint {

    public long lastCommittedOffset = -1; // -1 表示尚无任何提交
    public long committedCount = 0;
    public Map<String, Object> stateCommitted = new LinkedHashMap<>();

    /** 已准备未提交事务；null 表示当前没有进行中的事务。 */
    public PreparedTxn prepared = null;

    /** 两阶段提交的“准备”记录。 */
    public static final class PreparedTxn {
        public long txnId;
        public long endOffset;          // 该事务覆盖到的输入 offset
        public long beginOffset;        // 该事务起始输入 offset（含）
        public String outputFile;       // 接收器暂存文件名（相对 sink 目录）
        public String outputDigest;     // 暂存输出内容 SHA-256，防止落盘损坏/被篡改
        public Map<String, Object> stateAfter = new LinkedHashMap<>();

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("txnId", txnId);
            m.put("beginOffset", beginOffset);
            m.put("endOffset", endOffset);
            m.put("outputFile", outputFile);
            m.put("outputDigest", outputDigest);
            m.put("stateAfter", stateAfter);
            return m;
        }

        @SuppressWarnings("unchecked")
        public static PreparedTxn fromMap(Map<String, Object> m) {
            PreparedTxn p = new PreparedTxn();
            p.txnId = Json.lng(m, "txnId", 0);
            p.beginOffset = Json.lng(m, "beginOffset", 0);
            p.endOffset = Json.lng(m, "endOffset", 0);
            p.outputFile = Json.str(m, "outputFile", null);
            p.outputDigest = Json.str(m, "outputDigest", null);
            Object s = m.get("stateAfter");
            if (s instanceof Map) {
                p.stateAfter = (Map<String, Object>) s;
            }
            return p;
        }
    }

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("version", 1);
        m.put("lastCommittedOffset", lastCommittedOffset);
        m.put("committedCount", committedCount);
        m.put("stateCommitted", stateCommitted);
        m.put("prepared", prepared == null ? null : prepared.toMap());
        return m;
    }

    @SuppressWarnings("unchecked")
    public static Checkpoint fromMap(Map<String, Object> m) {
        Checkpoint cp = new Checkpoint();
        cp.lastCommittedOffset = Json.lng(m, "lastCommittedOffset", -1);
        cp.committedCount = Json.lng(m, "committedCount", 0);
        Object s = m.get("stateCommitted");
        if (s instanceof Map) {
            cp.stateCommitted = (Map<String, Object>) s;
        }
        Object p = m.get("prepared");
        if (p instanceof Map) {
            cp.prepared = PreparedTxn.fromMap((Map<String, Object>) p);
        }
        return cp;
    }

    /** 快照存储：单文件原子替换。 */
    public static final class Store {
        private final Path file;
        private final Path tmpFile;

        public Store(Path dataDir) {
            this.file = dataDir.resolve("checkpoint.json");
            this.tmpFile = dataDir.resolve("checkpoint.json.tmp");
        }

        public boolean exists() {
            return Files.exists(file);
        }

        public void save(Checkpoint cp) throws IOException {
            FileIO.writeAtomically(file, Json.writePretty(cp.toMap()));
        }

        /** 读取最新快照；首次启动（无文件）返回空快照。 */
        public Checkpoint load() throws IOException {
            if (!Files.exists(file)) {
                return new Checkpoint();
            }
            String text = FileIO.readString(file);
            try {
                return Checkpoint.fromMap(Json.parseObject(text));
            } catch (RuntimeException e) {
                // 理论上不会发生（原子写）；真遇到则按数据损坏明确报错，绝不静默重置
                throw new IOException("checkpoint.json 无法解析，为避免丢数据服务拒绝启动: " + e.getMessage(), e);
            }
        }

        public Path path() {
            return file;
        }

        public Path tmpPath() {
            return tmpFile;
        }
    }
}
