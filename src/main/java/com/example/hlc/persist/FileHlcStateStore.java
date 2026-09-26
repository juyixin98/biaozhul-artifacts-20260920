package com.example.hlc.persist;

import com.example.hlc.core.HlcTimestamp;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Optional;

/**
 * JSON-file backed {@link HlcStateStore}. Writes are atomic (temp file +
 * atomic move) so a crash mid-write cannot corrupt the previous state.
 */
public final class FileHlcStateStore implements HlcStateStore {

    private final Path file;
    private final ObjectMapper mapper;

    public FileHlcStateStore(Path file, ObjectMapper mapper) {
        this.file = file;
        this.mapper = mapper;
    }

    @Override
    public void save(HlcTimestamp timestamp) {
        try {
            Path parent = file.toAbsolutePath().getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Path tmp = file.resolveSibling(file.getFileName() + ".tmp");
            mapper.writeValue(tmp.toFile(), timestamp);
            try {
                Files.move(tmp, file, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (java.nio.file.AtomicMoveNotSupportedException e) {
                Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("failed to persist HLC state to " + file, e);
        }
    }

    @Override
    public Optional<HlcTimestamp> load() {
        if (!Files.exists(file)) {
            return Optional.empty();
        }
        try {
            return Optional.of(mapper.readValue(file.toFile(), HlcTimestamp.class));
        } catch (IOException e) {
            // Corrupt state file: start fresh rather than crash, but do not hide it.
            System.err.println("WARN: unreadable HLC state file " + file + ": " + e.getMessage());
            return Optional.empty();
        }
    }
}
