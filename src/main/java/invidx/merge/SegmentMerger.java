package invidx.merge;

import invidx.model.Document;
import invidx.segment.Segment;
import invidx.segment.SegmentWriter;
import invidx.store.Directory;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Pure merge computation + temp-file writing.
 *
 * <p>A merge keeps exactly one revision per document id — the highest
 * generation among the inputs, provided it carries no tombstone. All other
 * revisions (older and/or tombstoned) are physically dropped, which is how
 * deletes and superseded updates are reclaimed.
 *
 * <p>The merger only produces a {@code .seg.tmp} file. It never renames it
 * or touches the manifest, so an interrupted merge publishes nothing;
 * publication is the engine's responsibility.
 */
public final class SegmentMerger {

    /** A ready-to-publish merge: tmp file written and fsynced. */
    public record MergePlan(String targetName, Path tmpFile,
                            List<String> sourceNames, int docCount) {
    }

    private SegmentMerger() {
    }

    public static MergePlan build(Directory dir, String targetName,
                                  List<Segment> sources,
                                  Set<Long> deletedAsOfBuild) throws IOException {
        // Highest generation per id across all sources.
        Map<Integer, Long> latestGen = new HashMap<>();
        for (Segment seg : sources) {
            for (Map.Entry<Integer, Segment.Entry> e : seg.docs().entrySet()) {
                latestGen.merge(e.getKey(), e.getValue().gen(), Math::max);
            }
        }

        List<Document> kept = new ArrayList<>();
        for (Map.Entry<Integer, Long> e : latestGen.entrySet()) {
            int id = e.getKey();
            long gen = e.getValue();
            if (deletedAsOfBuild.contains(invidx.model.DocKey.encode(id, gen))) {
                continue;
            }
            String text = findText(sources, id, gen);
            kept.add(new Document(id, gen, text));
        }
        kept.sort(java.util.Comparator.comparing(Document::key));

        byte[] data = SegmentWriter.serialize(kept);
        Path tmp = SegmentWriter.writeTemp(dir, targetName, data);
        return new MergePlan(targetName, tmp,
                sources.stream().map(Segment::name).toList(), kept.size());
    }

    private static String findText(List<Segment> sources, int id, long gen) {
        for (Segment seg : sources) {
            Segment.Entry entry = seg.get(id);
            if (entry != null && entry.gen() == gen) {
                return entry.text();
            }
        }
        throw new IllegalStateException("lost text for id=" + id + " gen=" + gen);
    }
}
