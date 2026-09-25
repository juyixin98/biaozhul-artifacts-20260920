package com.example.seg.model;

/**
 * 一个切分单元（词或未知单字）。
 *
 * @param surface  字面文本
 * @param known    是否命中词典词
 * @param cost     该单元的放大整数代价
 * @param dictCost 命中词典时词典中声明的原始代价；未知字为 null
 */
public record Token(String surface, boolean known, long cost, java.math.BigDecimal dictCost) {

    public static Token known(String surface, long scaledCost, java.math.BigDecimal declaredCost) {
        return new Token(surface, true, scaledCost, declaredCost);
    }

    public static Token unknown(String surface, long scaledCost) {
        return new Token(surface, false, scaledCost, null);
    }
}
