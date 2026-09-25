package com.example.ac.test;

import com.example.ac.Pattern;

import java.util.ArrayList;
import java.util.List;

public final class TestUtils {

    private TestUtils() {
    }

    public static List<Pattern> patterns(String... literals) {
        List<Pattern> out = new ArrayList<>();
        for (int i = 0; i < literals.length; i++) {
            out.add(Pattern.of("p" + i, literals[i]));
        }
        return out;
    }

    public static List<Pattern> patternsNamed(List<String> ids, List<String> literals) {
        List<Pattern> out = new ArrayList<>();
        for (int i = 0; i < literals.size(); i++) {
            out.add(Pattern.of(ids.get(i), literals.get(i)));
        }
        return out;
    }
}
