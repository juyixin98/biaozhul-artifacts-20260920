package com.example.sessionwindow.time;

/** Injectable clock so nothing in the library reads the wall clock directly. */
public interface Clock {
    long currentTimeMillis();

    static Clock system() {
        return SystemClock.INSTANCE;
    }
}
