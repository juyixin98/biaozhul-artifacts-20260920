package com.example.smj;

import java.io.IOException;

/**
 * Forward-only cursor over a key-sorted row stream, backed either by an in-memory
 * list or by a k-way merge of spill files.
 */
public interface SortedCursor extends AutoCloseable {
  boolean hasNext();

  /** The current row; valid only while {@link #hasNext()} is true. */
  Row peek();

  /** Advance past the current row. */
  void advance() throws IOException;

  @Override
  void close();
}
