# Fixture: mutual recursion (DANGEROUS)
# Taint can only reach the sink AFTER traversing the unroll<->step
# recursive cycle: on entry n=3, the n<1 arm is not taken, so a single
# pass over the functions finds no source->sink path. The SCC fixpoint
# must iterate until the path "through the cycle" is discovered.

func unroll(n, data) {
    if (n < 1) {
        return step(n, data);
    }
    return unroll(n - 1, data);
}

func step(n, data) {
    if (n < 1) {
        sink(data);
        return 0;
    }
    return unroll(n - 1, data);
}

v = source();
unroll(3, v);
