package com.example.ac.corpus;

import java.util.List;

/** 语料描述：文本 + 模式（id 已分配）+ profile 名称与参数。 */
public record CorpusProfile(String name, String text, List<String> patternIds, List<String> patterns) {
}
