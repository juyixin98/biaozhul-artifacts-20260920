package booleansearch;

import booleansearch.index.InvertedIndex;
import booleansearch.index.Tokenizer;
import booleansearch.model.Document;

import java.util.List;
import java.util.TreeSet;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * 索引层测试：分词、增删恢复、倒排链对删除文档的排除、NOT 固定全集。
 */
public final class IndexTest {

    public static void run() {
        TestFramework.reset();
        TestFramework.section("IndexTest: 分词器");

        assertEquals(List.of("cat", "dog123", "tea"),
                Tokenizer.tokenize("  Cat,  dog123! -- tea?"),
                "标点分隔 + 小写化");
        assertEquals(List.of(), Tokenizer.tokenize(null), "null 输入返回空表");
        assertEquals(List.of("a", "b"), Tokenizer.tokenize("a中文b"),
                "非 ASCII 字符不作为词的一部分，会把两侧 ASCII 序列切开");

        TestFramework.section("IndexTest: 增删改与倒排链");

        InvertedIndex idx = new InvertedIndex();
        int id1 = idx.addDocument("标题一", "cat dog pet");
        int id2 = idx.addDocument("标题二", "cat food");
        int id3 = idx.addDocument("标题三", "dog pet tea");
        assertEquals(1, id1, "首个自动分配 id 为 1");
        assertEquals(3, idx.docCount(), "三篇存活文档");
        assertEquals(new TreeSet<>(List.of(1, 2)), idx.postings("cat"), "cat 倒排链");
        assertEquals(2, idx.postingsSize("cat"), "cat 倒排链大小");
        assertEquals(new TreeSet<Integer>(), idx.postings("zzz"), "未知词倒排链为空");
        assertTrue(!idx.termKnown("zzz"), "未知词 termKnown=false");
        assertTrue(idx.termKnown("cat"), "已知词 termKnown=true");

        // 删除文档 1：倒排链与全集都应收缩
        assertTrue(idx.delete(1), "删除存在的文档返回 true");
        assertTrue(!idx.delete(1), "重复删除返回 false");
        assertTrue(!idx.delete(999), "删除不存在 id 返回 false");
        assertEquals(2, idx.docCount(), "删除后存活 2 篇");
        assertEquals(1, idx.deletedCount(), "已删除计数为 1");
        assertEquals(new TreeSet<>(List.of(2)), idx.postings("cat"), "删除后 cat 倒排链排除文档 1");
        assertEquals(new TreeSet<>(List.of(3)), idx.postings("dog"), "删除后 dog 倒排链排除文档 1");
        assertEquals(new TreeSet<>(List.of(2, 3)), idx.liveDocIds(), "NOT 全集只剩 2,3");
        assertEquals(new TreeSet<>(List.of(1, 2, 3)), idx.allDocIdsEver(), "历史全集仍含 1");
        assertTrue(idx.termKnown("cat"), "删除文档后词项仍然已知（原始倒排表保留）");

        // 恢复文档 1：全部还原
        assertTrue(idx.restore(1), "恢复已删除文档返回 true");
        assertTrue(!idx.restore(1), "再次恢复返回 false");
        assertEquals(3, idx.docCount(), "恢复后存活 3 篇");
        assertEquals(new TreeSet<>(List.of(1, 2)), idx.postings("cat"), "恢复后倒排链还原");
        assertEquals(new TreeSet<>(List.of(1, 2, 3)), idx.liveDocIds(), "恢复后全集还原");

        // 用 putDocument 装确定性 id
        InvertedIndex idx2 = new InvertedIndex();
        idx2.putDocument(new Document(10, "十", "cat"));
        idx2.putDocument(new Document(20, "二十", "dog"));
        assertEquals(2, idx2.docCount(), "putDocument 指定 id");
        int auto = idx2.addDocument("自动", "fish");
        assertEquals(21, auto, "指定过较大 id 后，自增 id 接续其后");

        TestFramework.section("IndexTest: 标题与正文共同被索引");
        InvertedIndex idx3 = new InvertedIndex();
        idx3.addDocument("Coffee Guide", "only tea here");
        assertTrue(idx3.postings("coffee").contains(1), "标题中的词被索引");
        assertTrue(idx3.postings("tea").contains(1), "正文中的词被索引");

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("IndexTest 存在失败");
        }
    }
}
