# Fixture: clean program (SAFE) - constants only, no source reaches a sink

func add(a, b) {
    return a + b;
}

x = add(1, 2);
sink(x);
