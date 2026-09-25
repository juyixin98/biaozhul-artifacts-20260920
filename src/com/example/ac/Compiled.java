package com.example.ac;

import java.util.List;

/**
 * 编译结果：自动机 + 模式表 + 空模式相关元数据。
 *
 * @param patterns          全部输入模式（含被跳过的空模式），按原始序号排列
 * @param nonEmpty          参与自动机匹配的非空模式
 * @param emptyIndices      空模式的原始序号（按 {@link EmptyPatternPolicy} 决定用途）
 * @param skippedIndices    策略为 SKIP 时被忽略的空模式序号
 */
public record Compiled(List<Pattern> patterns,
                       List<Pattern> nonEmpty,
                       int[] emptyIndices,
                       int[] skippedIndices,
                       EmptyPatternPolicy policy,
                       Automaton automaton) {

    public Pattern patternAt(int index) {
        return patterns.get(index);
    }
}
