package com.example.positiondiff.search;

import java.util.List;

/**
 * Self-contained synthetic corpus — hand-authored local documents about a
 * fictional project, deliberately including Chinese and English content and
 * repeated lines. Nothing here is fetched from an external service.
 */
public final class Corpus {

    private Corpus() {}

    public static List<Doc> docs() {
        return List.of(
            new Doc("readme", "corpus/README.txt", "项目说明",
                "myers 差异算法项目\n"
                + "本项目提供带位置的文本行级差异计算。\n"
                + "支持 LF 与 CRLF 换行以及末尾换行。\n"
                + "差异结果可以在预算受限时显式降级。\n"
                + "降级脚本仍然可以应用并恢复目标文本。\n"),
            new Doc("server-doc", "corpus/SERVER.txt", "服务接口说明",
                "HTTP JSON service for line diff\n"
                + "The service exposes /api/diff and /api/search endpoints.\n"
                + "Requests and responses are JSON over localhost only.\n"
                + "A budget limits Myers search and may return a degraded result.\n"
                + "The degraded result is valid but not claimed shortest.\n"),
            new Doc("spec", "corpus/SPEC.md", "差异规格",
                "# 差异规格\n"
                + "换行形式是内容的一部分。\n"
                + "末尾换行是内容的一部分。\n"
                + "空文件没有任何逻辑行。\n"
                + "应用编辑序列必须恢复目标文本。\n"
                + "重复行必须得到稳定的最短编辑序列。\n"),
            new Doc("notes", "corpus/NOTES.log", "开发笔记",
                "2026-09-24 project bootstrap\n"
                + "2026-09-24 implement line splitter for LF and CRLF\n"
                + "2026-09-24 implement Myers with budget counters\n"
                + "2026-09-24 add fallback prefix suffix edit script\n"
                + "2026-09-24 randomized apply round trip tests pass\n"),
            new Doc("changelog", "corpus/CHANGELOG.txt", "changelog",
                "0.1.0 initial release\n"
                + "0.1.0 line level Myers diff\n"
                + "0.1.0 local search over synthetic corpus\n"
                + "0.1.0 JSON HTTP service\n"),
            new Doc("poem", "corpus/POEM.txt", "重复的行",
                "same line\n"
                + "same line\n"
                + "same line\n"
                + "different unique final line\n")
        );
    }
}
