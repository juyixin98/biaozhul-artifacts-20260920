package com.example.timeout.clock;

import java.time.Instant;

/**
 * 服务内唯一允许使用的时钟接口，刻意把两类读数分开：
 *
 * <ul>
 *   <li>{@link #wall()}：墙钟（wall clock），人类日历时间，可被 NTP/管理员向前或向后调整，
 *       <b>只能</b>用于在系统边界把“墙钟截止时间”换算成单调计时余量；</li>
 *   <li>{@link #monoNanos()}：单调计时器读数（类似 {@link System#nanoTime()}），
 *       只会随真实流逝时间前进，<b>所有已安排超时的时长判断只允许用它</b>。</li>
 * </ul>
 *
 * 严禁用两次 wall() 的差值来计算时长，也严禁把单调读数展示为日期时间——混用两种时钟是本项目要消除的缺陷本身。
 */
public interface TimeoutClock {

    /** 当前墙钟读数（UTC 瞬时），可能因校时而跳变。 */
    Instant wall();

    /** 当前单调计时器读数（纳秒，原点任意），保证单调非减。 */
    long monoNanos();

    /** 时钟实现类型，用于 API 响应与审计（system / virtual）。 */
    String type();
}
