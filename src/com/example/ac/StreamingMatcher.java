package com.example.ac;

import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CoderResult;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 流式多模式匹配器。
 *
 * <p>状态由两部分组成：AC 当前状态 + 全局 code point 计数，因此模式可以跨越任意块
 * 边界命中。块可以按 Java String（UTF-16）喂入，也可以按 UTF-8 字节喂入；字节路径
 * 自行处理跨块的多字节序列，块切在多字节字符中间不会导致错位或漏报。
 *
 * <p>重叠匹配：同一结束位置上沿 dictionary link 输出的所有模式（含不同长度、
 * 含重复模式）都会各产生一条 {@link Match}。
 *
 * <p>空模式在 {@link EmptyPatternPolicy#MATCH_EVERY_POSITION} 下于每个 code point
 * 边界产生一条零长度命中；长度 n 的流共有 n+1 个边界，无论怎么分块、块在何处切分，
 * 结果与整体匹配完全一致。
 *
 * <p>输出顺序为规范事件序（见 {@link Match#compareTo}）：先结束位置、再开始位置、
 * 最后模式序号；各块按序拼接即全局有序。
 */
public final class StreamingMatcher {

    private final Compiled compiled;
    private final Automaton auto;
    private final int[] activeEmpty;

    private int state;
    private int consumed;       // 已消费的 code point 数
    private int consumedChars;  // 已消费的 UTF-16 单元数
    private boolean started;
    private boolean finished;

    // UTF-8 跨块解码器；保留结尾不完整的多字节字节序列到下一块。
    private final java.nio.charset.CharsetDecoder decoder =
            StandardCharsets.UTF_8.newDecoder();
    private byte[] pending = new byte[0];

    public StreamingMatcher(Compiled compiled) {
        this.compiled = compiled;
        this.auto = compiled.automaton();
        this.activeEmpty = compiled.policy() == EmptyPatternPolicy.MATCH_EVERY_POSITION
                ? compiled.emptyIndices().clone()
                : new int[0];
    }

    /** 喂入一个文本块，命中结果追加到 {@code sink}。 */
    public void feed(String chunk, List<Match> sink) {
        if (finished) {
            throw new IllegalStateException("matcher already finished");
        }
        if (chunk == null || chunk.isEmpty()) {
            return;
        }
        if (!started) {
            emitEmpty(consumed, consumedChars, sink); // 位置 0
            started = true;
        }
        int len = chunk.length();
        int i = 0;
        while (i < len) {
            int cp = chunk.codePointAt(i);
            int cpChars = Character.charCount(cp);
            consume(cp, cpChars, sink);
            i += cpChars;
        }
    }

    /** 喂入一个 UTF-8 字节块（可在多字节字符中间切分）。 */
    public void feedBytes(byte[] chunk, int off, int len, List<Match> sink) {
        if (finished) {
            throw new IllegalStateException("matcher already finished");
        }
        if (len == 0) {
            return;
        }
        byte[] all = concat(pending, chunk, off, len);
        pending = new byte[0];

        ByteBuffer in = ByteBuffer.wrap(all);
        CharBuffer out = CharBuffer.allocate(all.length + 2); // UTF-8: 最坏 1 字节 -> 1 个 UTF-16 单元
        CoderResult result = decoder.decode(in, out, false);
        if (result.isError()) {
            throw new IllegalArgumentException("malformed UTF-8 input: " + result);
        }
        // in.remaining() 为结尾尚未构成完整字符的字节，留到下一块。
        if (in.hasRemaining()) {
            pending = new byte[in.remaining()];
            in.get(pending);
        }
        out.flip();
        String decoded = out.toString();
        if (!decoded.isEmpty()) {
            feed(decoded, sink);
        }
    }

    public void feedBytes(byte[] chunk, List<Match> sink) {
        feedBytes(chunk, 0, chunk.length, sink);
    }

    /** 结束流；返回尾部（最后边界上的空模式）命中。 */
    public List<Match> finish() {
        if (finished) {
            throw new IllegalStateException("matcher already finished");
        }
        finished = true;
        if (pending.length > 0) {
            // 最后再尝试一次解码（endOfInput=true），残字节会被判定为畸形序列。
            ByteBuffer in = ByteBuffer.wrap(pending);
            CharBuffer out = CharBuffer.allocate(pending.length + 2);
            CoderResult result = decoder.decode(in, out, true);
            if (!result.isError()) {
                result = decoder.flush(out);
            }
            if (result.isError() || out.position() == 0) {
                throw new IllegalArgumentException(
                        "malformed UTF-8 input: " + pending.length + " trailing byte(s) at end of stream");
            }
            out.flip();
            pending = new byte[0];
            List<Match> rest = new ArrayList<>();
            feed(out.toString(), rest);
            return rest;
        }
        if (!started) {
            // 空流：仍有位置 0 这一个边界。
            List<Match> tail = new ArrayList<>(activeEmpty.length);
            emitEmpty(0, 0, tail);
            started = true;
            return tail;
        }
        return Collections.emptyList();
    }

    private void consume(int cp, int cpUtf16Chars, List<Match> sink) {
        state = auto.go(state, cp);
        consumed++;
        consumedChars += cpUtf16Chars;

        // 非空输出：当前状态终止集 + dictionary link 链上的全部终止集。
        List<Integer> tmp = new ArrayList<>();
        int s = state;
        if (auto.nodes.get(s).terminals.length > 0) {
            for (int idx : auto.nodes.get(s).terminals) {
                tmp.add(idx);
            }
        }
        int dl = auto.nodes.get(s).dictLink;
        while (dl >= 0) {
            for (int idx : auto.nodes.get(dl).terminals) {
                tmp.add(idx);
            }
            dl = auto.nodes.get(dl).dictLink;
        }
        // 同一结束位置内部需要按 (start, patternIndex) 排序；
        // dictionary link 走向更短后缀（start 更大），先收集再排序。
        tmp.sort(this::compareTerminals);
        for (int idx : tmp) {
            sink.add(buildMatch(idx, consumed, consumedChars, false));
        }

        // 该 code point 之后的边界。
        emitEmpty(consumed, consumedChars, sink);
    }

    private int compareTerminals(int a, int b) {
        int la = compiled.patternAt(a).codePoints().length;
        int lb = compiled.patternAt(b).codePoints().length;
        int c = Integer.compare(la, lb); // 长模式 start 更小，先输出
        if (c != 0) {
            return -c; // start 升序 → 长度降序
        }
        return Integer.compare(a, b);
    }

    private void emitEmpty(int cpPos, int charPos, List<Match> sink) {
        for (int idx : activeEmpty) {
            sink.add(buildMatch(idx, cpPos, charPos, true));
        }
    }

    private Match buildMatch(int idx, int endCp, int endChar, boolean empty) {
        Pattern p = compiled.patternAt(idx);
        int mLen = empty ? 0 : p.codePoints().length;
        int charLen = empty ? 0 : p.utf16Length();
        return new Match(
                p.id(),
                endCp - mLen,
                endCp,
                endChar - charLen,
                endChar,
                idx,
                p.literal());
    }

    private static byte[] concat(byte[] a, byte[] b, int off, int len) {
        byte[] out = new byte[a.length + len];
        System.arraycopy(a, 0, out, 0, a.length);
        System.arraycopy(b, off, out, a.length, len);
        return out;
    }
}
