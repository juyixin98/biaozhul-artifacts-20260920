package com.example.sessionwindow;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Append-only JSON-lines log of durable decisions:
 *   {"type":"event", ...}      one accepted event
 *   {"type":"watermark","wm":N} manual watermark advancement
 *
 * Every append is flushed + fsynced before the API responds. Replaying the
 * whole file in order deterministically reconstructs engine state.
 */
public final class EventLog implements AutoCloseable {

    private final Path file;
    private OutputStream out;

    public EventLog(Path file) {
        try {
            Path parent = file.toAbsolutePath().getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            this.file = file;
            this.out = Files.newOutputStream(file,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND, StandardOpenOption.WRITE);
        } catch (IOException e) {
            throw new UncheckedIOException("Cannot open event log: " + file, e);
        }
    }

    public synchronized void append(Map<String, Object> record) {
        try {
            out.write(Json.write(record).getBytes(StandardCharsets.UTF_8));
            out.write('\n');
            out.flush();
            // fsync so an accepted decision survives power loss
            if (out instanceof java.io.FileOutputStream fos) {
                fos.getFD().sync();
            }
        } catch (IOException e) {
            throw new UncheckedIOException("Failed to append to event log", e);
        }
    }

    public synchronized List<Map<String, Object>> replay() {
        if (!Files.exists(file)) {
            return List.of();
        }
        var records = new ArrayList<Map<String, Object>>();
        try (BufferedReader reader = Files.newBufferedReader(file, StandardCharsets.UTF_8)) {
            String line;
            int lineNo = 0;
            while ((line = reader.readLine()) != null) {
                lineNo++;
                if (line.isBlank()) {
                    continue;
                }
                try {
                    records.add(Json.parseObject(line));
                } catch (RuntimeException e) {
                    throw new IllegalStateException(
                            "Corrupt event log at " + file + ":" + lineNo + " (" + e.getMessage()
                                    + "). Refusing to start; inspect/repair the log manually.", e);
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException("Failed to read event log", e);
        }
        return records;
    }

    public Path path() {
        return file;
    }

    /** Truncate the log and reopen it (used by the reset endpoint). */
    public synchronized void reset() {
        try {
            out.close();
            Files.deleteIfExists(file);
            out = Files.newOutputStream(file,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND, StandardOpenOption.WRITE);
        } catch (IOException e) {
            throw new UncheckedIOException("Failed to reset event log", e);
        }
    }

    @Override
    public synchronized void close() {
        try {
            out.close();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
