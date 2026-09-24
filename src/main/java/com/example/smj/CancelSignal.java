package com.example.smj;

/** Cooperative cancellation flag checked at loop boundaries throughout the engine. */
@FunctionalInterface
public interface CancelSignal {
  CancelSignal NONE = () -> false;

  boolean isCancelled();

  static void check(CancelSignal signal) {
    if (signal.isCancelled()) {
      throw new CancelledException();
    }
  }
}
