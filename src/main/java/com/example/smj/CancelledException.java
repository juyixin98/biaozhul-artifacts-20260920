package com.example.smj;

/** Thrown when a task observes its cancellation flag. Unchecked so it can cross IO code. */
public class CancelledException extends RuntimeException {
  public CancelledException() {
    super("cancelled by user");
  }
}
