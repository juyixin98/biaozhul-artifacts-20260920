package phrase.index;

import phrase.core.Analyzer;
import phrase.core.Token;
import phrase.doc.Doc;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 一次词项出现：全局位置 + 所属字段。
 * 全局位置把文档的有序字段拼成一条词项序列，
 * 字段 f 之前插入 {@code fieldGap} 个虚拟位置。
 */
public record Occ(int position, String field) {}
