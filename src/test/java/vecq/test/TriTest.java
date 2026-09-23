package vecq.test;

import vecq.Tri;

/** 三值逻辑真值表（Kleene 逻辑）。 */
public final class TriTest {

    public static void run() {
        Assert.eqInt(Tri.FALSE, Tri.not(Tri.TRUE), "NOT TRUE = FALSE");
        Assert.eqInt(Tri.TRUE, Tri.not(Tri.FALSE), "NOT FALSE = TRUE");
        Assert.eqInt(Tri.UNKNOWN, Tri.not(Tri.UNKNOWN), "NOT UNKNOWN = UNKNOWN");

        // AND：任一 FALSE 即 FALSE；否则全 TRUE 才 TRUE，其余 UNKNOWN
        Assert.eqInt(Tri.FALSE, Tri.and(Tri.FALSE, Tri.UNKNOWN), "FALSE AND UNKNOWN = FALSE");
        Assert.eqInt(Tri.UNKNOWN, Tri.and(Tri.TRUE, Tri.UNKNOWN), "TRUE AND UNKNOWN = UNKNOWN");
        Assert.eqInt(Tri.UNKNOWN, Tri.and(Tri.UNKNOWN, Tri.UNKNOWN), "UNKNOWN AND UNKNOWN = UNKNOWN");
        Assert.eqInt(Tri.TRUE, Tri.and(Tri.TRUE, Tri.TRUE), "TRUE AND TRUE = TRUE");

        // OR：任一 TRUE 即 TRUE；否则全 FALSE 才 FALSE，其余 UNKNOWN
        Assert.eqInt(Tri.TRUE, Tri.or(Tri.TRUE, Tri.UNKNOWN), "TRUE OR UNKNOWN = TRUE");
        Assert.eqInt(Tri.UNKNOWN, Tri.or(Tri.FALSE, Tri.UNKNOWN), "FALSE OR UNKNOWN = UNKNOWN");
        Assert.eqInt(Tri.FALSE, Tri.or(Tri.FALSE, Tri.FALSE), "FALSE OR FALSE = FALSE");
    }
}
