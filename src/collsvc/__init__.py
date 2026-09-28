"""collsvc — locale-aware Unicode sorting & range retrieval service.

A multi-module Python backend that sorts strings and answers range/prefix
queries using ICU collation (via PyICU), persists both the original text and
its ICU sort key in SQLite, and binds every collation option to an index
version so a rules upgrade can never mix old and new cursors.
"""

__version__ = "0.1.0"
