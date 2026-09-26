package com.example.monotime;

import java.time.Instant;

/**
 * 双时钟端口。系统中有两种时间，绝不能混用来度量同一个时长：
 *
 * <ul>
 *   <li><b>墙钟（wall clock）</b>：人类日历时间，会被 NTP/管理员/夏令时向前或向后调整；
 *       只用于在“转换边界”上表达截止时间。</li>
 *   <li><b>单调计时器（monotonic timer）</b>：{@code System.nanoTime()} 风格的流逝计时，
 *       不受墙钟调整影响；运行期间所有剩余时间/到期判断只用它。</li>
 * </ul>
 *
 * 唯一允许的跨域操作：在安排超时的一瞬间，用两个墙钟瞬间之差算出时长，
 * 把它加到当前单调读数上得到单调死线刻度。此后不再触碰墙钟。
 */
public interface ClockPort {

    /** 当前单调计时器读数（纳秒）。仅与同一 JVM 生命周期内的其它读数比较才有意义。 */
    long monotonicNanos();

    /** 当前墙钟瞬间（UTC 时间线上一点），可能因校时而跳变。 */
    Instant wallClockInstant();
}
