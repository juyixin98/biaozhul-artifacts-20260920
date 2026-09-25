package com.example.phrasesearch.model;

/**
 * 倒排表中的一条位置记录。
 *
 * @param docId         文档内部序号（文档加入索引的顺序）
 * @param position      全局位置（跨字段拼接流中的位置）或字段内局部位置，取决于检索模式
 * @param localPosition 该词元在其所属字段内的局部位置
 * @param field         词元所属字段名
 * @param startOffset   字段原文中的起始字符偏移（含）
 * @param endOffset     字段原文中的结束字符偏移（不含）
 */
public record Posting(int docId,
                      int position,
                      int localPosition,
                      String field,
                      int startOffset,
                      int endOffset) implements Comparable<Posting> {

    /** 先按文档、再按位置、最后按字段排序，保证枚举顺序确定。 */
    @Override
    public int compareTo(Posting o) {
        int c = Integer.compare(docId, o.docId);
        if (c != 0) {
            return c;
        }
        c = Integer.compare(position, o.position);
        if (c != 0) {
            return c;
        }
        return field.compareTo(o.field);
    }
}
