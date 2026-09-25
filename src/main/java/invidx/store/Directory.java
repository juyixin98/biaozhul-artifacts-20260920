package invidx.store;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.channels.FileChannel;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * File-system helpers implementing the durability primitives the index
 * relies on: create-new files, fsync of files and directories, and atomic
 * rename. All segment publication goes through these primitives.
 */
public final class Directory {

    private final Path root;

    public Directory(Path root) throws IOException {
        this.root = root;
        Files.createDirectories(root);
        fsyncDir(root);
    }

    public Path root() {
        return root;
    }

    public Path resolve(String name) {
        return root.resolve(name);
    }

    public Path resolve(Path base, String name) {
        return base.resolve(name);
    }

    /** Write bytes to a brand-new file (fails if it already exists), then fsync. */
    public void writeNewDurable(Path file, byte[] data) throws IOException {
        Files.createDirectories(file.getParent());
        try (FileChannel ch = FileChannel.open(file,
                StandardOpenOption.WRITE, StandardOpenOption.CREATE_NEW)) {
            ch.write(ByteBuffer.wrap(data));
            ch.force(true);
        }
    }

    /** Append bytes to an existing file and fsync. */
    public void appendDurable(Path file, byte[] data) throws IOException {
        try (FileChannel ch = FileChannel.open(file,
                StandardOpenOption.WRITE, StandardOpenOption.APPEND)) {
            ch.write(ByteBuffer.wrap(data));
            ch.force(true);
        }
    }

    public byte[] readAll(Path file) throws IOException {
        return Files.readAllBytes(file);
    }

    public boolean exists(Path file) {
        return Files.exists(file);
    }

    public void createDir(Path dir) throws IOException {
        Files.createDirectories(dir);
    }

    /** Atomic rename; fsyncs the parent directory afterwards. */
    public void atomicMove(Path from, Path to) throws IOException {
        try {
            Files.move(from, to, StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            // Fallback for exotic file systems; still a single rename(2) on POSIX.
            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING);
        }
        fsyncDir(to.getParent());
    }

    public void deleteRecursively(Path path) throws IOException {
        if (!Files.exists(path)) {
            return;
        }
        if (Files.isDirectory(path)) {
            try (DirectoryStream<Path> stream = Files.newDirectoryStream(path)) {
                for (Path child : stream) {
                    deleteRecursively(child);
                }
            }
        }
        Files.delete(path);
    }

    public void deleteIfExists(Path path) throws IOException {
        Files.deleteIfExists(path);
    }

    public void fsyncDir(Path dir) throws IOException {
        if (!Files.isDirectory(dir)) {
            return;
        }
        try (FileChannel ch = FileChannel.open(dir, StandardOpenOption.READ)) {
            ch.force(true);
        } catch (IOException ignored) {
            // Some platforms do not support fsync on directories; POSIX does.
        }
    }

    public static String sha256(byte[] data) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(data));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    public static byte[] utf8(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }
}
