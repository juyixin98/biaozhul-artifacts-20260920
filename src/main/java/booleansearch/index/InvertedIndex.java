package booleansearch.index;

import booleansearch.model.Document;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * 内存倒排索引。
 *
 * <p>核心数据：
 * <ul>
 *   <li>{@code postings}：词项 -> 包含该词的文档 id 集合（TreeSet，天然有序），
 *       倒排链本身在删除文档时<em>不</em>改写，查询时按删除标记过滤；</li>
 *   <li>{@code allDocuments}：装入过的全部文档（删除后仍保留，用于恢复）；</li>
 *   <li>{@code deletedIds}：被软删除的文档 id；NOT 的固定全集 =
 *       {@code allDocuments - deletedIds}（见 {@link #liveDocIds()}）。</li>
 * </ul>
 *
 * <p>所有公开方法都加 synchronized，HTTP 服务可多线程访问。
 */
public final class InvertedIndex {

    private final Map<String, TreeSet<Integer>> postings = new TreeMap<>();
    private final Map<Integer, Document> allDocuments = new TreeMap<>();
    private final TreeSet<Integer> deletedIds = new TreeSet<>();
    private int nextId = 1;

    /** 添加文档，自动分配 id。返回分配的 id。 */
    public synchronized int addDocument(String title, String text) {
        int id = nextId++;
        putDocument(new Document(id, title == null ? "" : title, text == null ? "" : text));
        return id;
    }

    /** 以指定 id 添加文档（用于测试/语料装载的确定性 id）。 */
    public synchronized void putDocument(Document doc) {
        allDocuments.put(doc.id(), doc);
        deletedIds.remove(doc.id());
        if (doc.id() >= nextId) {
            nextId = doc.id() + 1;
        }
        for (String term : Tokenizer.tokenize(doc.title() + "\n" + doc.text())) {
            postings.computeIfAbsent(term, k -> new TreeSet<>()).add(doc.id());
        }
    }

    /** 软删除：返回 true 表示该 id 此前是存活文档。 */
    public synchronized boolean delete(int id) {
        if (!allDocuments.containsKey(id) || deletedIds.contains(id)) {
            return false;
        }
        deletedIds.add(id);
        return true;
    }

    /** 恢复被软删除的文档：返回 true 表示该 id 此前确实处于删除状态。 */
    public synchronized boolean restore(int id) {
        if (!allDocuments.containsKey(id)) {
            return false;
        }
        return deletedIds.remove(id);
    }

    /** 返回存活文档；已删除文档返回 null。 */
    public synchronized Document getDocument(int id) {
        return deletedIds.contains(id) ? null : allDocuments.get(id);
    }

    /** 该词是否在倒排表中出现过（即使只出现在已删除文档中也算）。 */
    public synchronized boolean termKnown(String term) {
        return postings.containsKey(term);
    }

    /**
     * 词项的倒排链，已排除已删除文档。
     * 未知词返回空 TreeSet（调用方可直接用于集合运算）。
     */
    public synchronized TreeSet<Integer> postings(String term) {
        TreeSet<Integer> raw = postings.get(term);
        if (raw == null) {
            return new TreeSet<>();
        }
        TreeSet<Integer> live = new TreeSet<>(raw);
        live.removeAll(deletedIds);
        return live;
    }

    /** 倒排链大小（已排除删除文档），用于交集顺序优化的依据。 */
    public synchronized int postingsSize(String term) {
        TreeSet<Integer> raw = postings.get(term);
        if (raw == null) {
            return 0;
        }
        int size = raw.size();
        for (Integer deleted : deletedIds) {
            if (raw.contains(deleted)) {
                size--;
            }
        }
        return size;
    }

    /** 曾经装入的全部文档 id（含已删除），有序。 */
    public synchronized TreeSet<Integer> allDocIdsEver() {
        return new TreeSet<>(allDocuments.keySet());
    }

    /** NOT 使用的固定全集：当前存活的文档 id，有序。 */
    public synchronized TreeSet<Integer> liveDocIds() {
        TreeSet<Integer> live = new TreeSet<>(allDocuments.keySet());
        live.removeAll(deletedIds);
        return live;
    }

    public synchronized boolean isDeleted(int id) {
        return deletedIds.contains(id);
    }

    public synchronized List<Document> listDocuments() {
        List<Document> live = new ArrayList<>();
        for (Document doc : allDocuments.values()) {
            if (!deletedIds.contains(doc.id())) {
                live.add(doc);
            }
        }
        return live;
    }

    public synchronized int docCount() {
        return allDocuments.size() - deletedIds.size();
    }

    public synchronized int deletedCount() {
        return deletedIds.size();
    }

    public synchronized int termCount() {
        return (int) postings.keySet().stream()
                .filter(t -> postingsSize(t) > 0)
                .count();
    }

    public synchronized Map<String, Integer> stats() {
        Map<String, Integer> s = new TreeMap<>();
        s.put("documents", docCount());
        s.put("deletedDocuments", deletedIds.size());
        s.put("totalDocumentsEver", allDocuments.size());
        s.put("terms", termCount());
        s.put("vocabularyRaw", postings.size());
        return Collections.unmodifiableMap(s);
    }
}
