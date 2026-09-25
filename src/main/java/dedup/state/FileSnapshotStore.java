package dedup.state;

import dedup.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

/**
 * 文件快照存储：{@code snapshot.tmp} 写完后原子 {@code move} 覆盖 {@code snapshot.json}
 * （同目录 move 在本地文件系统上是原子的），保证崩溃时不会读到半份快照。
 */
public final class FileSnapshotStore implements SnapshotStore {

    private final Path file;
    private final Path tmp;

    public FileSnapshotStore(Path stateFile) {
        this.file = stateFile;
        this.tmp = stateFile.resolveSibling(stateFile.getFileName() + ".tmp");
    }

    @Override
    public synchronized void save(SnapshotData snapshot) throws IOException {
        Path parent = file.toAbsolutePath().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        String body = Json.write(snapshot.toJson());
        Files.writeString(tmp, body, StandardCharsets.UTF_8);
        try {
            Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException atomicFailed) {
            // 某些文件系统不支持 ATOMIC_MOVE，退回普通替换（本地目录仍近似崩溃安全）
            Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    @Override
    public synchronized SnapshotData load() throws IOException {
        if (!exists()) {
            return null;
        }
        String body = Files.readString(file, StandardCharsets.UTF_8);
        return SnapshotData.fromJson(Json.parse(body));
    }

    @Override
    public synchronized void clear() throws IOException {
        Files.deleteIfExists(file);
        Files.deleteIfExists(tmp);
    }

    @Override
    public synchronized boolean exists() {
        return Files.exists(file);
    }
}
