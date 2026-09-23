package joinplanner.model;

/** Kind of join a plan node performs. */
public enum JoinType {
    /** Equi-join on a listed edge. */
    INNER,
    /** Cartesian product joining two components of a disconnected graph. */
    CROSS
}
