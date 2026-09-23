package com.opp16.engine;

/** Thrown for invalid requests and invalid selection-vector subscripts. */
public class EngineException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public EngineException(String message) { super(message); }
    public EngineException(String message, Throwable cause) { super(message, cause); }
}
