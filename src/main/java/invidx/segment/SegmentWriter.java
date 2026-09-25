package invidx.segment;

import invidx.analyzer.Tokenizer;
import invidx.model.DocKey;
import invidx.model.Document;
import invidx.store.Directory;
import invidx.util.Codecs;
import invidx.util.LongList;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Serializes a batch of documents into a segment file.
 *
 * <p>The writer only ever creates a {@code .tmp} file and never publishes
 * it; publication (atomic rename + manifest swap) is the catalog's job.
 *
 * <p>File layout:
 * <pre>
 *   magic "IVSEG001\n"
 *   vlong numDocs
 *   stored fields: numDocs * (vlong id, vlong gen, vint byteLen, utf8)
 *   vlong termCount
 *   terms: termCount * (vint byteLen, utf8 term, vlong df, df * (vlong id, vlong gen))
 *   sha-256 hex (64 bytes) of everything above, then "IVEND\n"
 * </pre>
 * Postings are sorted by (id, gen); each document contributes at most one
 * posting per term even when the term repeats in its text.
 */
public final class SegmentWriter {

    static final byte[] MAGIC = "IVSEG001\n".getBytes(StandardCharsets.US_ASCII);
    static final byte[] END_MARKER = "IVEND\n".getBytes(StandardCharsets.US_ASCII);

    private SegmentWriter() {
    }

    /** Serialize to bytes (also used directly by crash-injection tests). */
    public static byte[] serialize(List<Document> documents) {
        List<Document> sortedDocs = documents.stream()
                .sorted(Comparator.comparing(Document::key))
                .toList();

        Map<String, long[]> termPostings = buildPostings(sortedDocs);

        ByteArrayOutputStream body = new ByteArrayOutputStream(1 << 16);
        body.writeBytes(MAGIC);
        putVLong(body, sortedDocs.size());
        for (Document doc : sortedDocs) {
            putVLong(body, doc.id() & 0xFFFFFFFFL);
            putVLong(body, doc.gen());
            byte[] textBytes = doc.text().getBytes(StandardCharsets.UTF_8);
            putVLong(body, textBytes.length);
            body.writeBytes(textBytes);
        }
        putVLong(body, termPostings.size());
        for (Map.Entry<String, long[]> e : termPostings.entrySet()) {
            byte[] termBytes = e.getKey().getBytes(StandardCharsets.UTF_8);
            putVLong(body, termBytes.length);
            body.writeBytes(termBytes);
            long[] p = e.getValue();
            putVLong(body, p.length);
            for (long key : p) {
                putVLong(body, DocKey.idOf(key) & 0xFFFFFFFFL);
                putVLong(body, DocKey.genOf(key));
            }
        }

        byte[] digest = sha256(body.toByteArray());
        byte[] hex = HexFormat.of().formatHex(digest).getBytes(StandardCharsets.US_ASCII);
        body.writeBytes(hex);
        body.writeBytes(END_MARKER);
        return body.toByteArray();
    }

    /** Write a fully serialized segment to {@code segments/&lt;name&gt;.seg.tmp}. */
    public static Path writeTemp(Directory dir, String name, byte[] data) throws java.io.IOException {
        Path segDir = dir.resolve("segments");
        dir.createDir(segDir);
        Path tmp = segDir.resolve(name + ".seg.tmp");
        dir.writeNewDurable(tmp, data);
        return tmp;
    }

    private static Map<String, long[]> buildPostings(List<Document> sortedDocs) {
        Map<String, LongList> acc = new HashMap<>();
        for (Document doc : sortedDocs) {
            long encoded = new DocKey(doc.id(), doc.gen()).encoded();
            for (String term : Tokenizer.tokenize(doc.text()).stream().distinct().toList()) {
                acc.computeIfAbsent(term, t -> new LongList()).add(encoded);
            }
        }
        TreeMap<String, long[]> out = new TreeMap<>();
        for (Map.Entry<String, LongList> e : acc.entrySet()) {
            long[] p = e.getValue().toArray();
            java.util.Arrays.sort(p);
            out.put(e.getKey(), p);
        }
        return out;
    }

    static void putVLong(ByteArrayOutputStream out, long value) {
        if (value < 0) {
            throw new IllegalArgumentException("negative vlong: " + value);
        }
        byte[] buf = new byte[Codecs.vLongSize(value)];
        Codecs.writeVLong(buf, 0, value);
        out.writeBytes(buf);
    }

    static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
