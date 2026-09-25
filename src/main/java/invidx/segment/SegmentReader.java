package invidx.segment;

import invidx.model.DocKey;
import invidx.store.Directory;
import invidx.util.Codecs;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;

/** Verifies the SHA-256 trailer and decodes a segment file into a {@link Segment}. */
public final class SegmentReader {

    private SegmentReader() {
    }

    public static Segment load(Directory dir, String name, boolean merged) throws IOException {
        return parse(name, merged, dir.readAll(segmentPath(dir, name)));
    }

    public static Path segmentPath(Directory dir, String name) {
        return dir.resolve("segments").resolve(name + ".seg");
    }

    /** Parse + verify bytes. */
    public static Segment parse(String name, boolean merged, byte[] data) {
        byte[] magic = SegmentWriter.MAGIC;
        if (data.length < magic.length + SegmentWriter.END_MARKER.length + 64) {
            throw new CorruptSegmentException(name, "file too short");
        }
        for (int i = 0; i < magic.length; i++) {
            if (data[i] != magic[i]) {
                throw new CorruptSegmentException(name, "bad magic");
            }
        }
        int endLen = SegmentWriter.END_MARKER.length;
        for (int i = 0; i < endLen; i++) {
            if (data[data.length - endLen + i] != SegmentWriter.END_MARKER[i]) {
                throw new CorruptSegmentException(name, "bad end marker");
            }
        }
        int digestPos = data.length - endLen - 64;
        String expectedHex = new String(data, digestPos, 64, StandardCharsets.US_ASCII);
        String actualHex = Directory.sha256(java.util.Arrays.copyOfRange(data, 0, digestPos));
        if (!expectedHex.equalsIgnoreCase(actualHex)) {
            throw new CorruptSegmentException(name, "checksum mismatch");
        }

        Codecs.Reader r = new Codecs.Reader(data, magic.length, digestPos);
        int numDocs = r.readVInt();
        Map<Integer, Segment.Entry> docs = new HashMap<>(numDocs * 2);
        for (int i = 0; i < numDocs; i++) {
            int id = (int) r.readVLong();
            long gen = r.readVLong();
            int textLen = r.readVInt();
            String text = r.readString(textLen);
            docs.put(id, new Segment.Entry(gen, text));
        }
        int termCount = r.readVInt();
        TreeMap<String, long[]> postings = new TreeMap<>();
        for (int t = 0; t < termCount; t++) {
            int termLen = r.readVInt();
            String term = r.readString(termLen);
            int df = r.readVInt();
            long[] p = new long[df];
            long lastId = -1;
            for (int i = 0; i < df; i++) {
                long id = r.readVLong();
                long gen = r.readVLong();
                if (id <= lastId && i > 0) {
                    throw new CorruptSegmentException(name, "postings not sorted");
                }
                lastId = id;
                p[i] = DocKey.encode((int) id, gen);
            }
            postings.put(term, p);
        }
        if (r.hasMore()) {
            throw new CorruptSegmentException(name, "trailing bytes in payload");
        }
        return new Segment(name, merged, docs, postings);
    }
}
