"""Toeplitz FFT backend package.

Layers (see README):
    config      - environment driven configuration and constants
    errors      - explicit failure categories (never collapse to success)
    logging_ctx - run identity / structured logging helpers
    inputs      - numeric input parsing and validation (system boundary)
    kernels     - direct, embedding, FFT compute kernels
    engine      - cached planning/dispatch, memory estimates, real/complex modes
    evidence    - independent oracle comparison and error reports
    service     - FastAPI application
"""

__version__ = "1.0.0"
