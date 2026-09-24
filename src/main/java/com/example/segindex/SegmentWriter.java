package com.example.segindex;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

/**
 * Writes one immutable segment to disk. The segment is built in a temp
 * directory and only becomes visible under its final name via an atomic
 * rename; it is published to readers only when the manifest (written
 * afterwards) references it. A crash at any point leaves either nothing or
 * an orphan directory that is cleaned on the next open.
 */
public final class SegmentWriter {

    static final String POSTINGS_FILE = "postings.json";
    static final String DOCS_FILE = "docs.json";

    private SegmentWriter() {
    }

    public static void write(Path indexDir, String segmentName,
                             Map<String, List<Posting>> postings,
                             Map<String, String> docs,
                             ObjectMapper mapper,
                             Consumer<String> crashHook) {
        Path tmpDir = indexDir.resolve(ManifestStore.TMP_SEGMENT_PREFIX + segmentName);
        Path finalDir = indexDir.resolve(segmentName);
        try {
            Files.createDirectories(tmpDir);
            mapper.writeValue(tmpDir.resolve(POSTINGS_FILE).toFile(), postings);
            IoUtil.fsync(tmpDir.resolve(POSTINGS_FILE));
            mapper.writeValue(tmpDir.resolve(DOCS_FILE).toFile(), docs);
            IoUtil.fsync(tmpDir.resolve(DOCS_FILE));
            IoUtil.fsyncDir(tmpDir);
            crashHook.accept("segment.beforeRename");
            ManifestStore.moveAtomic(tmpDir, finalDir);
            IoUtil.fsyncDir(indexDir);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to write segment " + segmentName, e);
        }
    }
}
