package invidx.catalog;

import com.fasterxml.jackson.databind.ObjectMapper;
import invidx.segment.SegmentInfo;
import invidx.store.Directory;

import java.io.IOException;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * Reads and atomically publishes {@code manifest.json}.
 *
 * <p>Publication always writes a fresh {@code manifest.json.tmp}, fsyncs it
 * and atomically renames it over {@code manifest.json}, followed by a
 * directory fsync. Therefore a reader opening the index either sees the
 * previous complete manifest or the new one — never a torn file.
 */
public final class ManifestStore {

    public static final String FILE_NAME = "manifest.json";
    private static final String TMP_NAME = "manifest.json.tmp";

    private final Directory dir;
    private final ObjectMapper mapper = new ObjectMapper();

    public ManifestStore(Directory dir) {
        this.dir = dir;
    }

    public boolean exists() {
        return dir.exists(dir.resolve(FILE_NAME));
    }

    public Manifest read() throws IOException {
        byte[] data = dir.readAll(dir.resolve(FILE_NAME));
        return mapper.readValue(data, Manifest.class);
    }

    public void write(long version, long segmentSeq, int nextDocId,
                      Map<Integer, Long> nextGen,
                      List<SegmentInfo> segments) throws IOException {
        Manifest m = new Manifest(version, segmentSeq, nextDocId,
                Map.copyOf(nextGen), List.copyOf(segments));
        byte[] data = mapper.writerWithDefaultPrettyPrinter().writeValueAsBytes(m);
        Path tmp = dir.resolve(TMP_NAME);
        dir.writeNewDurable(tmp, data);
        dir.atomicMove(tmp, dir.resolve(FILE_NAME));
    }

    /** Remove a possibly stale tmp file from an interrupted publication. */
    public void cleanupTemp() throws IOException {
        dir.deleteIfExists(dir.resolve(TMP_NAME));
    }
}
