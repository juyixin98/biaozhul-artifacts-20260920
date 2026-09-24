# Fixture: source inside a callee, sink at top level (DANGEROUS)

func get_input() {
    d = source();
    return d;
}

d = get_input();
sink(d);
