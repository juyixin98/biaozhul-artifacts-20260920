package com.example.editdistance;

import java.util.List;

/**
 * One verified search hit: the corpus term and its exact code-point edit distance
 * from the normalized query.
 */
public record Match(String term, int distance) {
}
