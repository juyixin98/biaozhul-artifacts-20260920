package com.example.ac;

import java.util.ArrayList;
import java.util.List;

/** 编译入口：模式列表 + 空模式策略 → 可复用的 {@link Compiled}。 */
public final class Engine {

    private Engine() {
    }

    public static Compiled compile(List<Pattern> input, EmptyPatternPolicy policy) {
        List<Pattern> patterns = new ArrayList<>(input.size());
        List<Pattern> nonEmpty = new ArrayList<>();
        List<Integer> empty = new ArrayList<>();
        List<Integer> skipped = new ArrayList<>();

        for (int i = 0; i < input.size(); i++) {
            Pattern p = input.get(i).withIndex(i);
            patterns.add(p);
            if (p.isEmpty()) {
                empty.add(i);
                if (policy == EmptyPatternPolicy.SKIP) {
                    skipped.add(i);
                }
            } else {
                nonEmpty.add(p);
            }
        }

        if (policy == EmptyPatternPolicy.ERROR && !empty.isEmpty()) {
            throw new IllegalArgumentException(
                    "empty pattern is not allowed under EmptyPatternPolicy.ERROR "
                    + "(offending indices: " + empty + ")");
        }

        Automaton automaton = new Automaton();
        for (Pattern p : nonEmpty) {
            automaton.insert(p.codePoints(), p.index());
        }
        automaton.build();

        return new Compiled(
                List.copyOf(patterns),
                List.copyOf(nonEmpty),
                empty.stream().mapToInt(Integer::intValue).toArray(),
                skipped.stream().mapToInt(Integer::intValue).toArray(),
                policy,
                automaton);
    }

    public static Compiled compileDefault(List<Pattern> input) {
        return compile(input, EmptyPatternPolicy.MATCH_EVERY_POSITION);
    }

    /** 一次性整体匹配（流式匹配的便捷封装）。 */
    public static List<Match> match(Compiled compiled, String text) {
        StreamingMatcher sm = new StreamingMatcher(compiled);
        List<Match> out = new ArrayList<>();
        sm.feed(text, out);
        out.addAll(sm.finish());
        return out;
    }
}
