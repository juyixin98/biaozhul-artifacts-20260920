package com.example.txflow;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 本地事务接收器（local transactional sink）。
 *
 * <p><b>作用域声明：</b>本接收器提供的“恰好一次（effectively exactly-once）”保证
 * <b>只覆盖本进程管理的本地文件接收器</b>（sink 目录下的 output 文件与 committed.log）。
 * 它<b>不</b>对任何外部系统（数据库、消息队列、远程 HTTP、其它进程写入的文件）
 * 提供事务保证——对端若不参与同一提交协议，重复发送在网络层面无法消除。
 *
 * <h3>可见性规则（接收器的核心不变量）</h3>
 * <pre>
 *   一条输出可见（出现在 outputs/ 且在 committed.log 中有提交标记）
 *     当且仅当它所在事务已经提交。
 * </pre>
 *
 * 目录布局：
 * <pre>
 *   sink/
 *     staging/                 私有暂存区，对外不可见
 *       txn-000007.out         prepare 阶段写入，fsync
 *     outputs/                 已发布输出
 *       txn-000007.out         staging 原子 rename 而来
 *     committed.log            提交标记日志：每行 "txnId,endOffset,sha256"，append+fsync
 * </pre>
 *
 * <h3>提交协议（每个事务）</h3>
 * <ol>
 *   <li>{@link #prepare}：输出写 staging/txn-N.out 并 fsync；rename 到 outputs/（原子发布）；
 *       返回摘要。此刻文件已存在但无提交标记 → 仍视为“未可见”。</li>
 *   <li>{@link #commit}：向 committed.log 追加一行提交标记并 fsync。追加是幂等的：
 *       若标记已存在则直接返回成功。</li>
 * </ol>
 * 崩溃恢复时以 committed.log 为唯一权威：无标记的 outputs/ 文件一律隐藏（回收），
 * 因此“先发布文件后写标记”的崩溃窗口不会产生重复或虚假可见输出。
 */
public class LocalTxnSink {

    private final Path sinkDir;
    private final Path stagingDir;
    private final Path outputsDir;
    private final Path markerLog;

    public LocalTxnSink(Path dataDir) throws IOException {
        this.sinkDir = dataDir.resolve("sink");
        this.stagingDir = sinkDir.resolve("staging");
        this.outputsDir = sinkDir.resolve("outputs");
        this.markerLog = sinkDir.resolve("committed.log");
        Files.createDirectories(stagingDir);
        Files.createDirectories(outputsDir);
        synchronized (this) {
            if (!Files.exists(markerLog)) {
                Files.createFile(markerLog);
            }
        }
        FileIO.fsyncParent(markerLog);
    }

    /** 暂存输出文件名（相对 sink 目录，使用 staging/ 前缀）。 */
    public static String stagingName(long txnId) {
        return "staging/txn-" + Long.toUnsignedString(txnId) + ".out";
    }

    /** 发布后输出文件名（相对 sink 目录，使用 outputs/ 前缀）。 */
    public static String outputName(long txnId) {
        return "outputs/txn-" + Long.toUnsignedString(txnId) + ".out";
    }

    private static String baseName(long txnId) {
        return "txn-" + Long.toUnsignedString(txnId) + ".out";
    }

    /**
     * prepare：写暂存文件（fsync）→ 原子 rename 到 outputs/。
     *
     * @return 输出内容的 SHA-256，写入快照用于恢复时比对
     */
    public synchronized String prepare(long txnId, byte[] content) throws IOException {
        String digest = sha256(content);
        Path staging = stagingDir.resolve(baseName(txnId));
        Path output = outputsDir.resolve(baseName(txnId));

        // 幂等：重试同一事务（recovered prepared txn 再次 prepare 不应发生，
        // 但防御性处理）——若 outputs 文件已存在且摘要一致，直接复用。
        if (Files.exists(output) && sha256(Files.readAllBytes(output)).equals(digest)
                && Files.size(output) == content.length) {
            Files.deleteIfExists(staging);
            return digest;
        }

        Files.write(staging, content,
                java.nio.file.StandardOpenOption.WRITE,
                java.nio.file.StandardOpenOption.CREATE,
                java.nio.file.StandardOpenOption.TRUNCATE_EXISTING,
                java.nio.file.StandardOpenOption.SYNC);
        try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(staging,
                java.nio.file.StandardOpenOption.WRITE, java.nio.file.StandardOpenOption.READ)) {
            ch.force(true);
        }
        Files.move(staging, output, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        FileIO.fsyncParent(outputsDir);
        return digest;
    }

    /**
     * commit：追加提交标记。幂等——标记已存在则不重复追加。
     *
     * 标记格式：txnId,endOffset,sha256
     */
    public synchronized void commit(long txnId, long endOffset, String digest) throws IOException {
        if (isCommitted(txnId)) {
            return; // 重放已提交事务：幂等，无重复标记
        }
        // 提交前确认输出文件确实在位（协议要求“先发布、后标记”）
        Path output = outputsDir.resolve(baseName(txnId));
        if (!Files.exists(output)) {
            throw new IOException("commit 失败：txn " + txnId + " 的输出文件不存在，拒绝写提交标记");
        }
        String line = txnId + "," + endOffset + "," + digest + "\n";
        FileIO.appendDurably(markerLog, line.getBytes(StandardCharsets.UTF_8));
    }

    /** 读取提交标记日志（权威已提交序列）。 */
    public synchronized List<Marker> readMarkers() throws IOException {
        List<Marker> markers = new ArrayList<>();
        if (!Files.exists(markerLog)) {
            return markers;
        }
        for (String line : Files.readAllLines(markerLog, StandardCharsets.UTF_8)) {
            if (line.isBlank()) {
                continue;
            }
            String[] parts = line.split(",", 3);
            if (parts.length != 3) {
                throw new IOException("committed.log 损坏：非法行 [" + line + "]");
            }
            markers.add(new Marker(Long.parseLong(parts[0].trim()),
                    Long.parseLong(parts[1].trim()), parts[2].trim()));
        }
        return markers;
    }

    public synchronized boolean isCommitted(long txnId) throws IOException {
        for (Marker m : readMarkers()) {
            if (m.txnId == txnId) {
                return true;
            }
        }
        return false;
    }

    /**
     * 恢复：以 committed.log 为权威，清理协议中间态遗留文件。
     *
     * @return 恢复动作描述（供管理接口/测试观察）
     */
    public synchronized Map<String, Object> recover() throws IOException {
        List<String> actions = new ArrayList<>();
        java.util.Set<Long> committed = new java.util.TreeSet<>();
        for (Marker m : readMarkers()) {
            if (!committed.add(m.txnId)) {
                throw new IOException("committed.log 损坏：txn " + m.txnId + " 出现重复标记");
            }
        }

        // 1) staging 中残留：prepare 写文件前/rename 前崩溃 → 删除（会按快照重放重建）
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(stagingDir, "*.out")) {
            for (Path p : ds) {
                actions.add("删除未发布暂存文件 " + stagingDir.relativize(p));
                Files.deleteIfExists(p);
            }
        }

        // 2) outputs 中存在但无提交标记：rename 后、写标记前崩溃 → 隐藏（删除）。
        //    若快照里登记为 prepared，引擎会在恢复后重新提交或重放。
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(outputsDir, "*.out")) {
            for (Path p : ds) {
                Long id = parseTxnId(p.getFileName().toString());
                if (id == null) {
                    actions.add("警告：outputs 中存在无法识别文件名 " + p.getFileName());
                    continue;
                }
                if (!committed.contains(id)) {
                    actions.add("隐藏未提交输出 " + outputsDir.relativize(p)
                            + "（rename 后崩溃、提交标记缺失）");
                    Files.deleteIfExists(p);
                }
            }
        }

        // 3) 有标记但文件丢失：严重不一致，明确报错而非静默补写
        for (Long id : committed) {
            if (!Files.exists(outputsDir.resolve(baseName(id)))) {
                throw new IOException("接收器不一致：txn " + id + " 已提交但输出文件缺失");
            }
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("committedTxns", committed.size());
        result.put("actions", actions);
        return result;
    }

    /** 读取所有已提交输出（按 txnId 升序拼接），每个事务一个字节数组。 */
    public synchronized List<byte[]> readCommittedOutputs() throws IOException {
        List<byte[]> out = new ArrayList<>();
        for (Marker m : readMarkers()) {
            Path p = outputsDir.resolve(baseName(m.txnId));
            byte[] content = Files.readAllBytes(p);
            if (!sha256(content).equals(m.digest)) {
                throw new IOException("输出文件 txn " + m.txnId + " 摘要与提交标记不符（数据损坏）");
            }
            out.add(content);
        }
        return out;
    }

    public Path markerLogPath() {
        return markerLog;
    }

    public Path outputsDir() {
        return outputsDir;
    }

    private static Long parseTxnId(String name) {
        // txn-<digits>.out
        if (!name.startsWith("txn-") || !name.endsWith(".out")) {
            return null;
        }
        try {
            return Long.parseLong(name.substring(4, name.length() - 4));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    static String sha256(byte[] content) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(content));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** 一条提交标记。 */
    public static final class Marker {
        public final long txnId;
        public final long endOffset;
        public final String digest;

        public Marker(long txnId, long endOffset, String digest) {
            this.txnId = txnId;
            this.endOffset = endOffset;
            this.digest = digest;
        }
    }
}
