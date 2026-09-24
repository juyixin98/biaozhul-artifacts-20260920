package com.example.smj;

/** A user-level error in a join request or in the input data (bad key, missing field, ...). */
public class JoinException extends RuntimeException {
  public JoinException(String message) {
    super(message);
  }
}
