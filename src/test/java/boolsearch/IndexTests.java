package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.eval.Evaluator;
import boolsearch.query.Parser;

import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/** 索引与边界语义测试：纯 NOT、未知词、删除文档、空全集。 */
public final class IndexTests {

    static void register(List<Case> cases) {
        cases.add(new Case("index: 纯 NOT 相对固定全集求补", IndexTests::pureNot));
        cases.add(new Case("index: 未知词 -> 空集；NOT 未知词 -> 全集", IndexTests::unknownTerm));
        cases.add(new Case("index: NOT NOT x == x", IndexTests::doubleNot));
        cases.add(new Case("index: 删除文档（倒排表与全集同步）", IndexTests::deletion));
        cases.add(new Case("index: 删除不存在文档返回 false", IndexTests::deleteMissing));
        cases.add(new Case("index: 空全集下 NOT -> 空集", IndexTests::emptyUniverse));
        cases.add(new Case("index: 覆盖同 id 文档", IndexTests::overwrite));
    }

    private static InvertedIndex sample() {
        InvertedIndex index = new InvertedIndex();
        index.addDocument(1, "apple banana");
        index.addDocument(2, "banana cherry");
        index.addDocument(3, "cherry");
        return index;
    }

    private static TreeSet<Integer> eval(InvertedIndex idx, String q) throws Exception {
        return new TreeSet<>(new Evaluator(idx, true).eval(Parser.parse(q)));
    }

    static void pureNot() throws Exception {
        InvertedIndex idx = sample();
        Check.eq(eval(idx, "NOT cherry"), new TreeSet<>(List.of(1)), "NOT cherry 应为全集减 {2,3}");
        Check.eq(eval(idx, "apple AND NOT banana"), new TreeSet<>(), "apple AND NOT banana 应为空");
    }

    static void unknownTerm() throws Exception {
        InvertedIndex idx = sample();
        Check.eq(eval(idx, "zzz"), new TreeSet<>(), "未知词应为空集");
        Check.eq(eval(idx, "NOT zzz"), new TreeSet<>(List.of(1, 2, 3)), "NOT 未知词应为全集");
        Check.eq(eval(idx, "apple AND zzz"), new TreeSet<>(), "与未知词相交应为空");
        Check.eq(eval(idx, "apple OR zzz"), new TreeSet<>(List.of(1)), "与未知词求并不变");
    }

    static void doubleNot() throws Exception {
        InvertedIndex idx = sample();
        Check.eq(eval(idx, "NOT NOT apple"), eval(idx, "apple"), "双重否定应等于原集合");
    }

    static void deletion() throws Exception {
        InvertedIndex idx = sample();
        Check.eq(eval(idx, "NOT cherry"), new TreeSet<>(List.of(1)), "删除前");
        Check.isTrue(idx.deleteDocument(1), "删除 doc 1");
        Check.eq(idx.posting("apple"), new TreeSet<>(), "apple 倒排表应为空");
        Check.eq(idx.df("apple"), 0, "apple df 应为 0");
        Check.eq(eval(idx, "NOT cherry"), new TreeSet<>(), "删除后全集收缩，NOT 结果应变小");
        Check.eq(eval(idx, "banana"), new TreeSet<>(List.of(2)), "banana 倒排表应移除 doc 1");
        // 重新加入
        idx.addDocument(1, "apple banana");
        Check.eq(eval(idx, "NOT cherry"), new TreeSet<>(List.of(1)), "重新加入后恢复");
    }

    static void deleteMissing() {
        InvertedIndex idx = sample();
        Check.isTrue(!idx.deleteDocument(99), "删除不存在文档应返回 false");
        Check.eq(idx.docCount(), 3, "文档数不应变化");
    }

    static void emptyUniverse() throws Exception {
        InvertedIndex idx = sample();
        idx.deleteDocument(1);
        idx.deleteDocument(2);
        idx.deleteDocument(3);
        Check.eq(idx.docCount(), 0, "全集应为空");
        Check.eq(eval(idx, "NOT apple"), new TreeSet<>(), "空全集下 NOT 应为空集");
        Check.eq(eval(idx, "apple OR banana"), new TreeSet<>(), "空全集下 OR 应为空集");
    }

    static void overwrite() throws Exception {
        InvertedIndex idx = sample();
        idx.addDocument(1, "kiwi"); // 覆盖 doc 1
        Check.eq(eval(idx, "apple"), new TreeSet<>(), "旧词应被移除");
        Check.eq(eval(idx, "kiwi"), new TreeSet<>(List.of(1)), "新词应生效");
        Check.eq(idx.docCount(), 3, "覆盖不应增加文档数");
    }
}
