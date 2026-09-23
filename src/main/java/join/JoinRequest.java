package join;

import java.nio.file.Path;

/** Validated parameters of a POST /join request. */
public final class JoinRequest {

    public final Path leftInput;
    public final Path rightInput;
    public final String leftKeyColumn;
    public final String rightKeyColumn;
    public final long memoryBudgetBytes;

    JoinRequest(Path leftInput, Path rightInput,
                String leftKeyColumn, String rightKeyColumn,
                long memoryBudgetBytes) {
        this.leftInput = leftInput;
        this.rightInput = rightInput;
        this.leftKeyColumn = leftKeyColumn;
        this.rightKeyColumn = rightKeyColumn;
        this.memoryBudgetBytes = memoryBudgetBytes;
    }
}
