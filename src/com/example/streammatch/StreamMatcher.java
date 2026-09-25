package com.example.streammatch;

import java.nio.charset.CharacterCodingException;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 流式 Aho-Corasick 匹配器：增量消费码点 / char / UTF-8 字节块，产出全局位置命中。
 *
 * <p>位置全部相对于逻辑流起点（<b>码点</b>单位）。无论分多少块、在任意 UTF-8 字节边界
 * 切开，也无论在代理对中间如何切 char 缓冲，最终命中集合都与一次性匹配整段文本完全一致。</p>
 *
 * <h3>两种喂入风格</h3>
 * <ol>
 *   <li><b>原子风格</b>：{@link #feedCodePoint} / {@link #feedCodePoints} /
 *       {@link #feedChars(String)} / {@link #feedBytes}。</li>
 *   <li><b>块风格</b>：{@link #feedChunk(String)} /
 *       {@link #feedChunkBytes(byte[], int, int, boolean)}。一个调用代表一个外部数据块。</li>
 * </ol>
 *
 * <p>两种风格产出的命中<b>集合完全一致</b>（位置只取决于全局码点下标，与分块无关）；
 * 差别仅在命中通过 {@link #drainMatches()} 可见的批次：块风格下，一个码点产生的命中
 * 归入交付它的那个块的批次。这正是 {@code /match/stream} 逐块报告所依赖的语义。</p>
 *
 * <h3>空模式语义</h3>
 * 空模式在长度 n 的流上有 n+1 个命中位置 p ∈ [0,n]（{@code Match(id,p,p,"")}），
 * 无论用哪种喂入风格或如何切块，最终集合恒为完整的 n+1 个：
 * <ul>
 *   <li>{@code SKIP}：不产出任何空命中（默认）。</li>
 *   <li>{@code BEFORE}：消费第 i 个码点前产出位置 i 的空命中；
 *       {@link #finish()} 时（消费完 n 个码点后）产出最后位置 n。</li>
 *   <li>{@code AFTER}：流首次开始时产出位置 0；消费第 i 个码点后产出位置 i+1；
 *       空流在 {@link #finish()} 时产出位置 0。</li>
 * </ul>
 *
 * <h3>UTF-8 边界</h3>
 * 字节块结尾不完整的 UTF-8 序列自动留到下一块拼接解码；畸形序列在 strict 模式下抛
 * {@link IllegalArgumentException}（宽松模式替换为 U+FFFD）。
 */
public final class StreamMatcher {

    private final AhoCorasick ac;

    private int state;
    /** 已消费码点数（= 下一个码点的全局下标）。 */
    private int consumed;
    private boolean finished;
    /** AFTER 策略：是否已产出过位置 0 的空命中。 */
    private boolean afterZeroEmitted;

    private byte[] pending = new byte[0];
    private char pendingHigh;
    private boolean hasPendingHigh;

    private final List<Match> batch = new ArrayList<>();
    /** 全量累积。 */
    final List<Match> allMatches = new ArrayList<>();

    public StreamMatcher(AhoCorasick ac) {
        this.ac = ac;
    }

    public int consumedCodePoints() {
        return consumed;
    }

    public boolean isFinished() {
        return finished;
    }

    // ------------------------------------------------------------------
    // 核心推进（不含空模式策略包装）
    // ------------------------------------------------------------------

    private void advance(int cp) {
        state = ac.go(state, cp);
        int[] outs = ac.outputsOf(state);
        for (int id : outs) {
            String p = ac.patternText(id);
            int plen = p.codePointCount(0, p.length());
            int start = consumed + 1 - plen;
            Match m = new Match(id, start, start + plen, p);
            batch.add(m);
            allMatches.add(m);
        }
        consumed++;
    }

    private void emitEmpty(int pos) {
        if (ac.emptyPolicy == EmptyPatternPolicy.SKIP) {
            return;
        }
        for (int id : ac.emptyIds) {
            Match m = new Match(id, pos, pos, "");
            batch.add(m);
            allMatches.add(m);
        }
    }

    private void beforeAtomic() {
        if (ac.emptyPolicy == EmptyPatternPolicy.BEFORE) {
            emitEmpty(consumed);
        } else if (ac.emptyPolicy == EmptyPatternPolicy.AFTER && !afterZeroEmitted) {
            emitEmpty(0);
            afterZeroEmitted = true;
        }
    }

    private void afterAtomic() {
        if (ac.emptyPolicy == EmptyPatternPolicy.AFTER) {
            emitEmpty(consumed);
        }
    }

    // ------------------------------------------------------------------
    // 原子风格：码点
    // ------------------------------------------------------------------

    public void feedCodePoint(int cp) {
        ensureOpen();
        beforeAtomic();
        advance(cp);
        afterAtomic();
    }

    public void feedCodePoints(int[] cps, int off, int len) {
        ensureOpen();
        for (int i = 0; i < len; i++) {
            feedCodePoint(cps[off + i]);
        }
    }

    public void feedCodePoints(int[] cps) {
        feedCodePoints(cps, 0, cps.length);
    }

    // ------------------------------------------------------------------
    // char / String
    // ------------------------------------------------------------------

    /**
     * 喂入 char 数组一段（原子风格）。高代理落在本次数据结尾时留待下次拼接；
     * 流尾仍未配对的高代理在 {@link #finish()} 时按孤立代理计为一个码点。
     */
    public void feedChars(char[] chars, int off, int len) {
        ensureOpen();
        int i = off;
        int end = off + len;

        if (hasPendingHigh) {
            if (i < end) {
                char c = chars[i++];
                if (Character.isLowSurrogate(c)) {
                    feedCodePoint(Character.toCodePoint(pendingHigh, c));
                } else {
                    feedCodePoint(pendingHigh); // 高代理后非低代理：高代理孤立
                    hasPendingHigh = false;
                    i--; // 当前 char 重新走正常流程
                }
                hasPendingHigh = false;
            }
            if (i >= end) {
                return;
            }
        }

        while (i < end) {
            char c = chars[i];
            if (Character.isHighSurrogate(c) && i + 1 < end) {
                char low = chars[i + 1];
                if (Character.isLowSurrogate(low)) {
                    feedCodePoint(Character.toCodePoint(c, low));
                    i += 2;
                    continue;
                }
            }
            if (Character.isHighSurrogate(c)) {
                if (i + 1 == end) {
                    pendingHigh = c;
                    hasPendingHigh = true;
                    i++;
                    continue;
                }
                feedCodePoint(c); // 高代理后不是低代理：孤立
                i++;
            } else if (Character.isLowSurrogate(c)) {
                feedCodePoint(c); // 孤立低代理
                i++;
            } else {
                feedCodePoint(c);
                i++;
            }
        }
    }

    public void feedChars(String s) {
        if (s == null || s.isEmpty()) {
            return;
        }
        feedChars(s.toCharArray(), 0, s.length());
    }

    // ------------------------------------------------------------------
    // 块风格
    // ------------------------------------------------------------------

    /** 块风格喂入字符串：块首（一次）产出 BEFORE 空命中，块内其余码点不再逐个产出。 */
    public void feedChunk(String s) {
        ensureOpen();
        if (s == null || s.isEmpty()) {
            return;
        }
        feedChunkChars(s.toCharArray(), 0, s.length());
    }

    /**
     * 块风格喂入 UTF-8 字节块。
     *
     * <p>BEFORE 块语义：若本块（连同上一块残留）实际解码出至少一个新码点，则在第一个
     * 新码点之前产出一次该全局位置的空命中；纯残留补全块不产出。最终空命中位置恰为
     * p ∈ [0,n]，与朴素集合一致。</p>
     */
    public void feedChunkBytes(byte[] b, int off, int len, boolean strict) {
        ensureOpen();
        if (b == null || len <= 0) {
            return;
        }
        feedBytesInternal(b, off, len, strict, true);
    }

    /** 原子风格喂入 UTF-8 字节块（每个解码出的码点仍是原子事件）。 */
    public void feedBytes(byte[] b, int off, int len, boolean strict) {
        ensureOpen();
        feedBytesInternal(b, off, len, strict, false);
    }

    public void feedBytes(byte[] b) {
        feedBytes(b, 0, b.length, true);
    }

    // ------------------------------------------------------------------
    // 内部：char 喂入（可选择原子或块语义）
    // ------------------------------------------------------------------

    private void feedChunkChars(char[] chars, int off, int len) {
        int i = off;
        int end = off + len;

        if (hasPendingHigh) {
            if (i < end) {
                char c = chars[i++];
                if (Character.isLowSurrogate(c)) {
                    chunkAdvance(Character.toCodePoint(pendingHigh, c));
                } else {
                    chunkAdvance(pendingHigh);
                    hasPendingHigh = false;
                    i--;
                }
                hasPendingHigh = false;
            }
            if (i >= end) {
                return;
            }
        }

        while (i < end) {
            char c = chars[i];
            if (Character.isHighSurrogate(c) && i + 1 < end
                    && Character.isLowSurrogate(chars[i + 1])) {
                chunkAdvance(Character.toCodePoint(c, chars[i + 1]));
                i += 2;
            } else if (Character.isHighSurrogate(c) && i + 1 == end) {
                pendingHigh = c;
                hasPendingHigh = true;
                i++;
            } else {
                chunkAdvance(c);
                i++;
            }
        }
    }

    /**
     * 块语义下推进一个码点。BEFORE 空命中在每个码点前产出（位置只取决于全局码点下标，
     * 因而无论怎样分块，最终集合恒为完整的 p ∈ [0,n]）；AFTER 同理在每个码点后产出。
     * 两种喂入风格的<b>集合</b>一致，区别仅在命中随哪个 {@code drainMatches()} 批次可见。
     */
    private void chunkAdvance(int cp) {
        if (ac.emptyPolicy == EmptyPatternPolicy.BEFORE) {
            emitEmpty(consumed);
        } else if (ac.emptyPolicy == EmptyPatternPolicy.AFTER && !afterZeroEmitted) {
            emitEmpty(0);
            afterZeroEmitted = true;
        }
        advance(cp);
        if (ac.emptyPolicy == EmptyPatternPolicy.AFTER) {
            emitEmpty(consumed);
        }
    }

    // ------------------------------------------------------------------
    // 内部：UTF-8 字节处理
    // ------------------------------------------------------------------

    private void feedBytesInternal(byte[] b, int off, int len, boolean strict, boolean chunkMode) {
        if (b == null || len <= 0) {
            return;
        }
        byte[] data;
        int dataLen;
        int base;
        if (pending.length > 0) {
            data = new byte[pending.length + len];
            System.arraycopy(pending, 0, data, 0, pending.length);
            System.arraycopy(b, off, data, pending.length, len);
            dataLen = data.length;
            base = 0;
            pending = new byte[0];
        } else {
            data = b;
            dataLen = len;
            base = off;
        }

        int safeEnd = base + dataLen;
        int back = trailingIncompleteSize(data, base, dataLen, strict);
        int decodeEnd = safeEnd - back;
        if (back > 0) {
            pending = new byte[back];
            System.arraycopy(data, decodeEnd, pending, 0, back);
        }

        if (decodeEnd > base) {
            CharsetDecoder dec = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(strict ? CodingErrorAction.REPORT : CodingErrorAction.REPLACE)
                    .onUnmappableCharacter(strict ? CodingErrorAction.REPORT : CodingErrorAction.REPLACE);
            try {
                String s = dec.decode(java.nio.ByteBuffer.wrap(data, base, decodeEnd - base)).toString();
                if (!s.isEmpty()) {
                    if (chunkMode) {
                        feedChunkChars(s.toCharArray(), 0, s.length());
                    } else {
                        feedChars(s);
                    }
                }
            } catch (CharacterCodingException e) {
                throw new IllegalArgumentException("畸形 UTF-8 输入: " + e.getMessage(), e);
            }
        }
    }

    /**
     * 计算字节范围结尾“不完整 UTF-8 序列”的字节数（需要留待下一块）。
     * strict 模式下对明确畸形的尾部（孤立连续字节、非法前导、过长序列）直接抛异常。
     */
    private int trailingIncompleteSize(byte[] data, int base, int len, boolean strict) {
        int safeEnd = base + len;
        int last = data[safeEnd - 1] & 0xFF;
        if ((last & 0x80) == 0) {
            return 0; // ASCII 结尾
        }
        if ((last & 0xC0) == 0x80) {
            int t = 1;
            while (t < len && (data[safeEnd - 1 - t] & 0xC0) == 0x80) {
                t++;
            }
            if (t >= len) {
                if (strict) {
                    throw new IllegalArgumentException(
                            "畸形 UTF-8：块尾有 " + t + " 个无前导字节的连续字节（10xxxxxx）");
                }
                return 0;
            }
            int leadPos = safeEnd - 1 - t;
            int lead = data[leadPos] & 0xFF;
            int need = leadNeed(lead);
            if (need < 0) {
                if (strict) {
                    throw new IllegalArgumentException(
                            "非法 UTF-8 前导字节 0x" + Integer.toHexString(lead)
                                    + "，位于字节偏移 " + (leadPos - base));
                }
                return 0;
            }
            int have = t + 1;
            if (have < need) {
                return have;
            }
            if (have > need && strict) {
                throw new IllegalArgumentException(
                        "畸形 UTF-8：偏移 " + (leadPos - base) + " 的序列需要 "
                                + need + " 字节，实际有 " + have + " 字节");
            }
            return 0;
        }
        // 块尾本身是前导字节
        int need = leadNeed(last);
        if (need < 0) {
            if (strict) {
                throw new IllegalArgumentException(
                        "非法 UTF-8 前导字节 0x" + Integer.toHexString(last)
                                + "，位于块尾偏移 " + (len - 1));
            }
            return 0;
        }
        return 1;
    }

    /** UTF-8 前导字节声明的序列长度；非法前导（0xF8+）返回 -1。 */
    private static int leadNeed(int u) {
        if ((u & 0x80) == 0) {
            return 1;
        }
        if ((u & 0xE0) == 0xC0) {
            return 2;
        }
        if ((u & 0xF0) == 0xE0) {
            return 3;
        }
        if ((u & 0xF8) == 0xF0) {
            return 4;
        }
        return -1;
    }

    // ------------------------------------------------------------------
    // 结束 / 取结果
    // ------------------------------------------------------------------

    /** 声明流结束：校验残留并补齐结尾空命中。可重复调用（幂等）。 */
    public List<Match> finish() {
        if (finished) {
            return List.of();
        }
        finished = true;
        if (pending.length > 0) {
            throw new IllegalStateException(
                    "流结束时仍有 " + pending.length + " 个未完成的 UTF-8 字节残留");
        }
        if (hasPendingHigh) {
            // 流尾孤立高代理：按一个码点处理；两种风格在此汇合
            beforeAtomic();
            advance(pendingHigh);
            afterAtomic();
            hasPendingHigh = false;
            // 补上结尾位置 n 的 BEFORE 空命中（afterAtomic 不负责 BEFORE）
            if (ac.emptyPolicy == EmptyPatternPolicy.BEFORE) {
                emitEmpty(consumed);
            }
            return drainMatches();
        }
        if (ac.emptyPolicy == EmptyPatternPolicy.BEFORE) {
            emitEmpty(consumed);
        } else if (ac.emptyPolicy == EmptyPatternPolicy.AFTER && !afterZeroEmitted) {
            emitEmpty(0);
            afterZeroEmitted = true;
        }
        return drainMatches();
    }

    /** 取走自上次 drain 以来新增的命中。 */
    public List<Match> drainMatches() {
        if (batch.isEmpty()) {
            return new ArrayList<>();
        }
        List<Match> out = new ArrayList<>(batch);
        batch.clear();
        return out;
    }

    private void ensureOpen() {
        if (finished) {
            throw new IllegalStateException("匹配器已 finish，不能继续喂入");
        }
    }
}
