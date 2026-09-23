package joinplanner.model;

/** A base relation participating in the join. */
public record Table(String name, long rows) {
}
