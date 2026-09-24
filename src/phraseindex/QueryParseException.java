package phraseindex;

/** Thrown when a query string cannot be parsed. Carries a 0-based character position. */
public final class QueryParseException extends RuntimeException {

    private final int position;

    public QueryParseException(String message, int position) {
        super(message + " (at position " + position + ")");
        this.position = position;
    }

    public int position() {
        return position;
    }
}
