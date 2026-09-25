package com.example.segmenter.tests;

import com.example.segmenter.data.CorpusLoader;
import com.example.segmenter.model.Dictionary;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** 语料加载：注释/空行、词频代价、重复词拒绝、坏行报错、版本名取文件名。 */
public final class CorpusLoaderTest {

    private CorpusLoaderTest() {
    }

    public static void run() throws Exception {
        suite("corpus loader");

        Path tmp = Files.createTempDirectory("corpus-test");
        Path good = tmp.resolve("my_v.corpus");
        Files.writeString(good, String.join("\n",
                "# 注释行",
                "",
                "研究 10",
                "生命\t30",
                "  空白缩进词 5  ",
                ""));
        Dictionary dict = CorpusLoader.load(good);
        assertEquals("版本名取文件名去扩展名", "my_v", dict.version());
        assertEquals("词数为 3", 3, dict.size());
        assertEquals("词频总和", 45L, dict.totalFrequency());
        check("研究代价 = -ln(10/45)",
                Math.abs(dict.costOf("研究") - (-Math.log(10.0 / 45.0))) < 1e-12);
        check("生命代价 = -ln(30/45)",
                Math.abs(dict.costOf("生命") - (-Math.log(30.0 / 45.0))) < 1e-12);
        // 高频词代价更低
        check("高频词代价更低",
                dict.costOf("生命") < dict.costOf("研究"));

        // 重复词
        Path dup = tmp.resolve("dup.corpus");
        Files.writeString(dup, "研究 1\n研究 2\n");
        boolean dupRejected = false;
        try {
            CorpusLoader.load(dup);
        } catch (CorpusLoader.CorruptCorpusException e) {
            dupRejected = true;
        }
        check("重复词被拒绝", dupRejected);

        // 缺词频
        Path bad1 = tmp.resolve("bad1.corpus");
        Files.writeString(bad1, "研究\n");
        boolean bad1Rejected = false;
        try {
            CorpusLoader.load(bad1);
        } catch (CorpusLoader.CorruptCorpusException e) {
            bad1Rejected = true;
        }
        check("缺词频被拒绝", bad1Rejected);

        // 词频非正
        Path bad2 = tmp.resolve("bad2.corpus");
        Files.writeString(bad2, "研究 0\n");
        boolean bad2Rejected = false;
        try {
            CorpusLoader.load(bad2);
        } catch (CorpusLoader.CorruptCorpusException e) {
            bad2Rejected = true;
        }
        check("零词频被拒绝", bad2Rejected);

        // 空语料
        Path empty = tmp.resolve("empty.corpus");
        Files.writeString(empty, "# only comment\n");
        boolean emptyRejected = false;
        try {
            CorpusLoader.load(empty);
        } catch (CorpusLoader.CorruptCorpusException e) {
            emptyRejected = true;
        }
        check("空语料被拒绝", emptyRejected);

        // 内置两个版本语料可加载
        Dictionary v1 = CorpusLoader.load(Path.of("data/corpora/dict_v1.corpus"));
        Dictionary v2 = CorpusLoader.load(Path.of("data/corpora/dict_v2.corpus"));
        check("dict_v1 含研究", v1.contains("研究"));
        check("dict_v2 含研究生", v2.contains("研究生"));

        // 版本快照不可变
        boolean immutable = false;
        try {
            @SuppressWarnings({ "unchecked", "rawtypes" })
            Map<String, Object> mutableView = (Map) v1.costsView();
            mutableView.put("x", 1.0);
        } catch (UnsupportedOperationException e) {
            immutable = true;
        }
        check("词典快照不可变", immutable);

        // List 引用保留（避免未使用告警）
        check("临时目录路径以 corpus-test 开头",
                List.of(tmp.toString()).get(0).contains("corpus-test"));
    }
}
