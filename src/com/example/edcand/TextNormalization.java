package com.example.edcand;

import java.text.Normalizer;
import java.text.Normalizer.Form;

/**
 * 文本规范化策略。
 *
 * <p><b>为什么必须规范化：</b>像 {@code é} 这样的带重音字符有两种 Unicode 表示——
 * 预组合 {@code U+00E9}（NFC）与 {@code e + U+0301}（NFD，基字母 + 组合重音符）。
 * 二者视觉相同，但码点序列不同，直接算 Levenshtein 会得到距离 1。
 *
 * <p><b>本项目策略：</b>
 * <ul>
 *   <li>{@link #NFC}（默认）：索引构建和查询两侧统一先做 NFC，再做大小写折叠
 *       （{@link String#toLowerCase()}，按码点处理，不是按字节）。
 *       NFC 把组合字符序列合并为预组合形式，使视觉相同的文本码点序列一致。</li>
 *   <li>{@link #NFD}：需要“组合标记单独参与编辑”的场景可在请求里显式指定。</li>
 *   <li>{@link #NFKC}/{@link #NFKD}：兼容分解，会把全角字符、连字、兼容数字
 *       映射到规范等价形式（例如全角 "Ａ" → "A"，连字 "ﬁ" → "fi"）。</li>
 *   <li>{@link #NONE}：完全不规范化，原样按码点比较（用于验证原始行为）。</li>
 * </ul>
 *
 * <p>无论选哪种形式，规范化都只在“码点序列”层面进行；距离单位始终是码点。
 * 规范化<b>不改变</b>“以码点而非字节计数”这一保证：emoji 等增补平面字符
 * 在 NFC/NFD 下保持不变，码点数仍为 1（组合字符与字素簇的区别见 README）。
 */
public enum TextNormalization {
    NONE(null),
    NFC(Form.NFC),
    NFD(Form.NFD),
    NFKC(Form.NFKC),
    NFKD(Form.NFKD);

    private final Form form;

    TextNormalization(Form form) {
        this.form = form;
    }

    /**
     * 规范化文本：先 Unicode 规范化，再做大小写折叠。
     * NONE 模式原样返回。
     */
    public String apply(String raw) {
        String s = (form == null) ? raw : Normalizer.normalize(raw, form);
        if (this != NONE) {
            s = s.toLowerCase();
        }
        return s;
    }

    /** 解析请求参数，无法识别时抛 IllegalArgumentException。 */
    public static TextNormalization parse(String name) {
        if (name == null || name.isEmpty()) {
            return NFC;
        }
        return TextNormalization.valueOf(name.trim().toUpperCase());
    }
}
