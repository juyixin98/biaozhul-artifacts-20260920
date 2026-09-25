#!/usr/bin/env python3
"""Convenience wrapper: run all automated tests from the project root."""
import os
import sys
import unittest

if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, "tests"), top_level_dir=root)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
