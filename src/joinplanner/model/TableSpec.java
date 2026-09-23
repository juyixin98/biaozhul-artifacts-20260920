package joinplanner.model;

/**
 * One base relation.
 *
 * @param name       table name, unique, non-empty
 * @param rows       cardinality (number of rows); non-negative finite value
 * @param unique     join keys of this table are declared unique (primary-key side)
 * @param alias      optional display alias, defaults to name
 */
public record TableSpec(String name, double rows, boolean unique, String alias) {

    public String display() {
        return alias == null || alias.isBlank() ? name : alias;
    }
}
