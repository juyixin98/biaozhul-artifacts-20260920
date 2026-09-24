package com.example.smj;

import java.io.IOException;

/** Destination for joined row pairs. */
public interface RowSink {
  void emit(Row left, Row right) throws IOException;
}
