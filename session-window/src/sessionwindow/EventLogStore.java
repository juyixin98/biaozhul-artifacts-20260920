package sessionwindow;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Append-only persistence for <em>accepted</em> events.
 *
 * Each line is one JSON object: {"eventId":..., "key":..., "timestamp":...}.
 * Rejected events are deliberately not persisted: on restart the watermark is
 * re-derived from the accepted events (maxTs - allowedLateness), which makes
 * recovery deterministic — the replayed changelog, session ids and versions
 * come out identical to the original run.
 *
 * Durability: every append is flushed to the OS before the HTTP response is
 * returned (write() + flush()). This survives process crashes; it does not
 * guarantee a disk-level fsync, which is sufficient and honestly scoped for a
 * single-host demo.
 */
final class EventLogStore {

    private static final Logger LOG = Logger.getLogger(EventLogStore.class.getName());

    private final Path file;
    private final BufferedWriter writer;

    EventLogStore(Path dataDir) throws IOException {
        Files.createDirectories(dataDir);
        this.file = dataDir.resolve("events.log");
        this.writer = Files.newBufferedWriter(
                file,
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.APPEND);
    }

    /** Append one accepted event and flush it to the OS. */
    synchronized void append(String eventId, String key, long timestamp) throws IOException {
        Map<String, Object> rec = new LinkedHashMap<>();
        rec.put("eventId", eventId);
        rec.put("key", key);
        rec.put("timestamp", timestamp);
        writer.write(Json.write(rec));
        writer.write('\n');
        writer.flush();
    }

    /** Read all stored events in file order; arrival sequence is reassigned. */
    static List<SessionAggregator.Event> readAll(Path dataDir) {
        Path f = dataDir.resolve("events.log");
        List<SessionAggregator.Event> events = new ArrayList<>();
        if (!Files.exists(f)) {
            return events;
        }
        long seq = 0;
        try {
            for (String line : Files.readAllLines(f, StandardCharsets.UTF_8)) {
                if (line.isBlank()) {
                    continue;
                }
                Map<String, Object> rec = Json.parseObject(line);
                String eventId = Json.requireString(rec, "eventId");
                String key = Json.requireString(rec, "key");
                long ts = Json.requireLong(rec, "timestamp");
                events.add(new SessionAggregator.Event(eventId, key, ts, ++seq));
            }
        } catch (IOException e) {
            throw new IllegalStateException("Failed reading event log " + f, e);
        }
        return events;
    }

    synchronized void close() {
        try {
            writer.flush();
            writer.close();
        } catch (IOException e) {
            LOG.log(Level.WARNING, "Error closing event log", e);
        }
    }

    Path getFile() {
        return file;
    }
}
