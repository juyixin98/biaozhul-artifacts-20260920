package com.example.segindex;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Comparator;
import java.util.HashSet;
import java.util.Set;

/**
 * Loads and atomically persists the {@link Manifest}. Also responsible for
 * removing on-disk garbage (orphan segment dirs, temp dirs) that a crash may
 * have left behind.
 */
public final class ManifestStore {

    static final String MANIFEST_FILE = "manifest.json";
    static final String TMP_SUFFIX = ".tmp";
    static final String TMP_SEGMENT_PREFIX = "_tmp_";

    private final Path dir;
    private final ObjectMapper mapper;

    public ManifestStore(Path dir) {
        this.dir = dir;
        this.mapper = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
    }

    public ObjectMapper mapper() {
        return mapper;
    }

    public Manifest load() {
        Path file = dir.resolve(MANIFEST_FILE);
        if (!Files.exists(file)) {
            return new Manifest();
        }
        try {
            return mapper.readValue(file.toFile(), Manifest.class);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to read manifest from " + file, e);
        }
    }

    /** Atomically replaces the manifest: write tmp file, fsync, rename, fsync dir. */
    public void save(Manifest manifest) {
        Path tmp = dir.resolve(MANIFEST_FILE + TMP_SUFFIX);
        Path target = dir.resolve(MANIFEST_FILE);
        try {
            mapper.writeValue(tmp.toFile(), manifest);
            IoUtil.fsync(tmp);
            moveAtomic(tmp, target);
            IoUtil.fsyncDir(dir);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to persist manifest", e);
        }
    }

    /**
     * Deletes segment directories not referenced by the manifest and any
     * leftover temp directories. Called once at open time; after this the
     * on-disk state exactly matches the manifest.
     */
    public void cleanupOrphans(Manifest manifest) {
        Set<String> live = new HashSet<>(manifest.segments);
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir)) {
            for (Path child : stream) {
                String name = child.getFileName().toString();
                boolean orphanSegment = Files.isDirectory(child)
                        && name.startsWith(Index.SEGMENT_PREFIX) && !live.contains(name);
                boolean tempDir = Files.isDirectory(child) && name.startsWith(TMP_SEGMENT_PREFIX);
                boolean tmpManifest = name.equals(MANIFEST_FILE + TMP_SUFFIX);
                if (orphanSegment || tempDir || tmpManifest) {
                    deleteRecursively(child);
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException("failed to clean orphan files in " + dir, e);
        }
    }

    static void moveAtomic(Path tmp, Path target) throws IOException {
        try {
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    static void deleteRecursively(Path path) throws IOException {
        if (!Files.exists(path)) {
            return;
        }
        try (var walk = Files.walk(path)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.delete(p);
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
            });
        }
    }
}
