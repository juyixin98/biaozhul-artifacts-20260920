package com.example.neardup;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Deterministic synthetic corpus. Fixed seed, fixed structure — the same
 * documents are produced on every run, so clustering results and stats are
 * reproducible.
 *
 * <p>Structure:
 * <ul>
 *   <li>6 topics × (1 base + 2 mutated variants) — expected near-dup clusters
 *       of size 3 (shingle Jaccard of variants ≈ 0.6–0.8).</li>
 *   <li>A 5-document transitive chain: doc i shares ~2/3 of its shingles
 *       with doc i+1 (>= threshold) but the two endpoints share ~0.1
 *       (&lt; threshold). Connected-components semantics must still put all
 *       five in one cluster.</li>
 *   <li>Short-document counterexamples: two IDENTICAL two-word documents
 *       (fewer tokens than the shingle size → empty shingle sets → never
 *       clustered), plus one unique three-word document.</li>
 *   <li>5 distractors with disjoint vocabularies.</li>
 * </ul>
 */
public final class CorpusGenerator {

    public static final long CORPUS_SEED = 20260922L;

    private static final List<String> FILLER = List.of(
            "the", "a", "of", "and", "to", "in", "is", "it", "for", "on",
            "with", "as", "at", "by", "an", "be", "this", "that", "from", "or");

    private static final List<List<String>> TOPIC_WORDS = List.of(
            List.of("river", "delta", "sediment", "current", "bank", "flood", "channel",
                    "estuary", "basin", "tide", "gravel", "meander", "runoff", "watershed",
                    "erosion", "deposit", "stream", "valley", "marsh", "tributary"),
            List.of("compiler", "parser", "lexer", "bytecode", "syntax", "grammar",
                    "token", "optimizer", "runtime", "linker", "symbol", "register",
                    "backend", "frontend", "assembly", "interpreter", "codegen", "macro",
                    "recursion", "stack"),
            List.of("chord", "melody", "rhythm", "tempo", "harmony", "fugue", "sonata",
                    "cadence", "timbre", "octave", "counterpoint", "arpeggio", "syncopation",
                    "modulation", "resonance", "staccato", "legato", "overture", "refrain", "scale"),
            List.of("neuron", "synapse", "cortex", "axon", "dendrite", "myelin",
                    "receptor", "impulse", "ganglion", "reflex", "stimulus", "plasticity",
                    "membrane", "potential", "transmitter", "pathway", "lesion", "spike",
                    "network", "signal"),
            List.of("harvest", "soil", "irrigation", "crop", "fallow", "plow", "seed",
                    "furrow", "compost", "acre", "yield", "pasture", "tractor", "silo",
                    "drainage", "graft", "orchard", "tillage", "loam", "stubble"),
            List.of("nebula", "quasar", "pulsar", "galaxy", "orbit", "eclipse", "comet",
                    "asteroid", "supernova", "redshift", "parallax", "magnetar", "exoplanet",
                    "cluster", "void", "flare", "horizon", "apogee", "transit", "cosmos"));

    private static final int BASE_TOKENS = 100;
    private static final double FILLER_FRACTION = 0.25;
    private static final double REPLACE_PROB = 0.05;
    private static final double DELETE_PROB = 0.02;
    private static final int CHAIN_SENTENCES = 9;
    private static final int CHAIN_SENTENCE_TOKENS = 14;
    private static final int CHAIN_WINDOW = 5;
    private static final int CHAIN_DOCS = 5;

    private CorpusGenerator() {
    }

    public static List<Document> generate() {
        Random random = new Random(CORPUS_SEED);
        List<Document> docs = new ArrayList<>();

        // Topic clusters: base + two mutated variants each.
        for (int topic = 0; topic < TOPIC_WORDS.size(); topic++) {
            List<String> base = randomText(TOPIC_WORDS.get(topic), BASE_TOKENS, random);
            docs.add(new Document("topic" + topic + "-base", String.join(" ", base)));
            for (int variant = 1; variant <= 2; variant++) {
                docs.add(new Document("topic" + topic + "-v" + variant,
                        String.join(" ", mutate(base, TOPIC_WORDS.get(topic), random))));
            }
        }

        // Transitive chain: sliding 5-sentence windows over 9 sentences.
        List<List<String>> sentences = new ArrayList<>();
        for (int s = 0; s < CHAIN_SENTENCES; s++) {
            List<String> sentence = new ArrayList<>();
            for (int w = 0; w < CHAIN_SENTENCE_TOKENS; w++) {
                sentence.add("chain" + s + "word" + w);
            }
            sentences.add(sentence);
        }
        for (int d = 0; d < CHAIN_DOCS; d++) {
            List<String> tokens = new ArrayList<>();
            for (int s = d; s < d + CHAIN_WINDOW; s++) {
                tokens.addAll(sentences.get(s));
            }
            docs.add(new Document("chain-" + d, String.join(" ", tokens)));
        }

        // Short-document counterexamples (< shingle size tokens).
        docs.add(new Document("short-a", "hello world"));
        docs.add(new Document("short-b", "hello world")); // identical to short-a
        docs.add(new Document("short-c", "tiny note here"));

        // Distractors with disjoint vocabularies.
        for (int d = 0; d < 5; d++) {
            List<String> tokens = new ArrayList<>();
            for (int w = 0; w < 60; w++) {
                tokens.add("distract" + d + "tok" + random.nextInt(200));
            }
            docs.add(new Document("distractor-" + d, String.join(" ", tokens)));
        }
        return List.copyOf(docs);
    }

    private static List<String> randomText(List<String> topicWords, int length, Random random) {
        List<String> tokens = new ArrayList<>(length);
        for (int i = 0; i < length; i++) {
            if (random.nextDouble() < FILLER_FRACTION) {
                tokens.add(FILLER.get(random.nextInt(FILLER.size())));
            } else {
                tokens.add(topicWords.get(random.nextInt(topicWords.size())));
            }
        }
        return tokens;
    }

    private static List<String> mutate(List<String> base, List<String> topicWords, Random random) {
        List<String> mutated = new ArrayList<>(base.size());
        for (String token : base) {
            double roll = random.nextDouble();
            if (roll < DELETE_PROB) {
                continue;
            }
            if (roll < DELETE_PROB + REPLACE_PROB) {
                mutated.add(topicWords.get(random.nextInt(topicWords.size())));
            } else {
                mutated.add(token);
            }
        }
        return mutated;
    }
}
