package booleansearch.model;

/**
 * 一篇文本文档：不可变的 id + 标题 + 正文。
 */
public record Document(int id, String title, String text) {
}
