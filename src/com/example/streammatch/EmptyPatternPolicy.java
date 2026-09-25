package com.example.streammatch;

/**
 * 空模式（长度为 0 的模式）处理策略。
 *
 * <p>空模式在长度为 n（码点）的文本中的每个“间隙”位置 p ∈ [0, n] 都可以命中，
 * 共 n+1 个位置，命中记录为 {@code Match(id, p, p, "")}。三种策略决定空命中相对于
 * 普通字符/普通命中的<b>产出时机</b>（全部产出的集合相同，区别在流式语义）：</p>
 *
 * <ul>
 *   <li>{@link #SKIP}   —— 忽略所有空模式，不产出任何空命中（默认）。</li>
 *   <li>{@link #BEFORE} —— 每消费一个码点之前先产出该位置的空命中；
 *       流结束（finish）时再产出最后一个位置 n 的空命中。</li>
 *   <li>{@link #AFTER}  —— 流首次开始时产出位置 0 的空命中；
 *       每消费一个码点之后产出位置 i+1 的空命中。空流在 finish 时产出位置 0。</li>
 * </ul>
 */
public enum EmptyPatternPolicy {
    SKIP,
    BEFORE,
    AFTER
}
