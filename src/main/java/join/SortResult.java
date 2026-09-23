package join;

import java.util.List;

/** Outcome of {@link ExternalSorter#sort}: a readable sorted source plus spill statistics. */
public final class SortResult {

    public final ExternalSorter.SortedSource source;

    /** Run files backing the source (empty when served fully from memory). */
    public final List<java.nio.file.Path> runFiles;

    public final long totalRows;
    public final long nullRows;
    public final boolean spilled;
    public final int initialRuns;
    public final int mergePasses;

    /** Bytes of the run files produced in phase 1. */
    public final long initialSpillBytes;

    /** All temp bytes ever written (initial runs + intermediate multi-pass merges). */
    public final long totalTempBytesWritten;

    SortResult(ExternalSorter.SortedSource source,
               List<java.nio.file.Path> runFiles,
               long totalRows, long nullRows, boolean spilled,
               int initialRuns, int mergePasses,
               long initialSpillBytes, long totalTempBytesWritten) {
        this.source = source;
        this.runFiles = runFiles;
        this.totalRows = totalRows;
        this.nullRows = nullRows;
        this.spilled = spilled;
        this.initialRuns = initialRuns;
        this.mergePasses = mergePasses;
        this.initialSpillBytes = initialSpillBytes;
        this.totalTempBytesWritten = totalTempBytesWritten;
    }
}
