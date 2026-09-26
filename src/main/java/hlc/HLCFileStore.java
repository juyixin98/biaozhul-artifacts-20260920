package hlc;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;

/**
 * File-based persistence of one or more node clocks.
 *
 * <p>The on-disk format is a small UTF-8 Java properties file:
 * <pre>
 * version=1
 * tzdb=2024a
 * node.alice=1700000001000000:3
 * node.bob=1700000002500000:1
 * </pre>
 * Node keys are namespaced under {@code node.}; timestamps use the canonical
 * {@code <l>:<c>} form ({@link HLCTimestamp#parse}). Writes go to a temporary file and are
 * atomically moved into place, so a crash never leaves a torn state file behind.
 *
 * <p>Durability note: this store issues {@code fsync} before the rename. It is sufficient
 * for the local fixed-data backend; it is not a replicated or transactional log.
 */
public final class HLCFileStore {

    static final String KEY_VERSION = "version";
    static final String KEY_TZDB = "tzdb";
    static final String NODE_PREFIX = "node.";
    static final int FORMAT_VERSION = 1;

    private final Path file;

    public HLCFileStore(Path file) {
        this.file = file;
    }

    public Path path() {
        return file;
    }

    /** Atomically persist node name -> timestamp. */
    public void save(Map<String, HLCTimestamp> clocks) {
        Properties props = new Properties();
        props.setProperty(KEY_VERSION, Integer.toString(FORMAT_VERSION));
        props.setProperty(KEY_TZDB, TimeZoneInfo.version());
        // Deterministic order for readable files and stable samples.
        clocks.entrySet().stream()
                .sorted(Map.Entry.comparingByKey())
                .forEach(e -> props.setProperty(NODE_PREFIX + e.getKey(), e.getValue().toString()));

        Path parent = file.toAbsolutePath().getParent();
        try {
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Path tmp = Files.createTempFile(parent, ".hlc-", ".tmp");
            try (Writer w = Files.newBufferedWriter(tmp, StandardCharsets.UTF_8)) {
                props.store(w, "Hybrid Logical Clock state (tzdb version recorded above)");
            }
            // Make payload durable before it becomes the visible state.
            try (java.nio.channels.FileChannel ch =
                         java.nio.channels.FileChannel.open(tmp,
                                 java.nio.file.StandardOpenOption.WRITE)) {
                ch.force(true);
            }
            Files.move(tmp, file, java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            throw new HLCException("failed to persist HLC state to " + file, e);
        }
    }

    /** Load persisted state. Returns an empty map if the file does not exist yet. */
    public Map<String, HLCTimestamp> load() {
        if (!Files.exists(file)) {
            return new LinkedHashMap<>();
        }
        Properties props = new Properties();
        try (BufferedReader r = Files.newBufferedReader(file, StandardCharsets.UTF_8)) {
            props.load(r);
        } catch (IOException e) {
            throw new HLCException("failed to read HLC state from " + file, e);
        }

        String version = props.getProperty(KEY_VERSION);
        if (version != null && !Integer.toString(FORMAT_VERSION).equals(version)) {
            throw new HLCException("unsupported HLC state file version: " + version
                    + " (supported: " + FORMAT_VERSION + ")");
        }

        Map<String, HLCTimestamp> result = new LinkedHashMap<>();
        for (String name : props.stringPropertyNames()) {
            if (name.startsWith(NODE_PREFIX)) {
                String node = name.substring(NODE_PREFIX.length());
                if (node.isBlank()) {
                    throw new HLCException("invalid empty node name in state file");
                }
                result.put(node, HLCTimestamp.parse(props.getProperty(name)));
            }
        }
        return result;
    }
}
