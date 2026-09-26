package bitemporal.store;

import bitemporal.error.ValidationException;
import bitemporal.json.JsonMapper;
import bitemporal.model.TemporalRecord;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.List;

/**
 * 带本地 JSON 文件快照的存储：每次提交/重置后原子落盘，进程启动时整体恢复，
 * 使多个 CLI 进程（seed → 修订 → as-of 查询）共享同一份数据。
 *
 * <p>纯演示用途：单进程访问、整库快照，无并发写优化。</p>
 */
public class PersistentBitemporalStore extends BitemporalStore {

    private static final ObjectMapper MAPPER = JsonMapper.get();

    private final Path dbFile;

    public PersistentBitemporalStore(Path dbFile) {
        this.dbFile = dbFile;
        load();
    }

    public Path dbFile() {
        return dbFile;
    }

    @Override
    public synchronized bitemporal.model.CommitResult commit(
            bitemporal.model.TransactionRequest request) {
        var result = super.commit(request);
        persist();
        return result;
    }

    @Override
    public synchronized void reset() {
        super.reset();
        persist();
    }

    private void load() {
        if (!Files.exists(dbFile)) {
            return;
        }
        try {
            Snapshot snapshot = MAPPER.readValue(Files.readAllBytes(dbFile), Snapshot.class);
            if (snapshot.rows() != null) {
                loadSnapshot(snapshot.rows(), snapshot.lastCommitAt());
            }
        } catch (IOException e) {
            throw new UncheckedIOException("failed to read bitemporal db file " + dbFile, e);
        }
    }

    private void persist() {
        try {
            Path parent = dbFile.toAbsolutePath().getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Snapshot snapshot = new Snapshot(allRows(), lastCommitAt().orElse(null));
            byte[] bytes = MAPPER.writerWithDefaultPrettyPrinter().writeValueAsBytes(snapshot);

            Path tmp = dbFile.resolveSibling(dbFile.getFileName() + ".tmp");
            Files.write(tmp, bytes);
            try {
                Files.move(tmp, dbFile, StandardCopyOption.ATOMIC_MOVE);
            } catch (IOException atomicNotSupported) {
                Files.move(tmp, dbFile, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            throw new ValidationException("failed to persist db file " + dbFile + ": " + e.getMessage());
        }
    }

    private record Snapshot(List<TemporalRecord> rows, Instant lastCommitAt) {
    }
}
