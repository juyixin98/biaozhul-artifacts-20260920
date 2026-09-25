package invidx.segment;

import invidx.model.Document;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class SegmentFormatTest {

    private List<Document> docs() {
        return List.of(
                new Document(1, 1, "the quick brown fox"),
                new Document(2, 1, "quick lazy dog"),
                new Document(3, 7, "fox fox fox and hound 42"),
                new Document(0x7FFFFFFF, 1, "unicode: café naïve 日本語"));
    }

    @Test
    void roundTripsDocumentsAndPostings(@TempDir Path tmp) throws Exception {
        invidx.store.Directory dir = new invidx.store.Directory(tmp);
        byte[] data = SegmentWriter.serialize(docs());
        Path file = SegmentWriter.writeTemp(dir, "seg_000001", data);
        dir.atomicMove(file, SegmentReader.segmentPath(dir, "seg_000001"));

        Segment seg = SegmentReader.load(dir, "seg_000001", false);
        assertEquals(4, seg.docCount());
        assertEquals("the quick brown fox", seg.get(1).text());
        assertEquals(7L, seg.get(3).gen());

        long[] quick = seg.postings("quick");
        assertEquals(2, quick.length);
        assertEquals(1, invidx.model.DocKey.idOf(quick[0]));
        assertEquals(2, invidx.model.DocKey.idOf(quick[1]));

        // Repeated term in a doc yields a single posting.
        long[] fox = seg.postings("fox");
        assertEquals(2, fox.length);
        assertEquals(3, invidx.model.DocKey.idOf(fox[1]));
        assertEquals(7, invidx.model.DocKey.genOf(fox[1]));

        assertNull(seg.postings("missing"));
    }

    @Test
    void rejectsBitFlippedPayload() {
        byte[] data = SegmentWriter.serialize(docs());
        data[20] ^= 0x01;
        assertThrows(CorruptSegmentException.class,
                () -> SegmentReader.parse("seg_x", false, data));
    }

    @Test
    void rejectsTruncatedFile() {
        byte[] data = SegmentWriter.serialize(docs());
        byte[] cut = new byte[data.length - 50];
        System.arraycopy(data, 0, cut, 0, cut.length);
        assertThrows(CorruptSegmentException.class,
                () -> SegmentReader.parse("seg_x", false, cut));
    }
}
