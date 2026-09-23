package joinplanner.core;

import java.util.List;

/**
 * Raised when the join graph has more than one connected component and cross
 * products were not explicitly allowed. Mapped to HTTP 422 (the request is
 * well-formed but no single inner-join plan exists without Cartesian products).
 */
public class DisconnectedGraphException extends RuntimeException {

    private static final long serialVersionUID = 1L;
    private final List<List<String>> components;

    public DisconnectedGraphException(List<List<String>> components) {
        super("join graph is disconnected: " + components.size() + " components");
        this.components = components;
    }

    public List<List<String>> components() {
        return components;
    }
}
