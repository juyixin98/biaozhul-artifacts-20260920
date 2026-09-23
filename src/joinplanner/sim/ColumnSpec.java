package joinplanner.sim;

/** Request-side description of one generated join column. */
public final class ColumnSpec {
    public final String distribution;
    public final long ndv;
    public final double skew;         // zipf exponent
    public final double hotFraction; // hotkey share for value 1
    public final long[] frequencies; // explicit frequencies distribution
    public final boolean unique;
    public final int offset;         // values are shifted by this amount (disjoint value domains)
    public long[] generated;         // produced values (set by the simulator)

    public ColumnSpec(String distribution, long ndv, double skew, double hotFraction,
                      long[] frequencies, boolean unique, int offset) {
        this.distribution = distribution;
        this.ndv = ndv;
        this.skew = skew;
        this.hotFraction = hotFraction;
        this.frequencies = frequencies;
        this.unique = unique;
        this.offset = offset;
    }
}
