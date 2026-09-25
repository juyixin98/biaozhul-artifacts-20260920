"""Sparse vector cosine-similarity retrieval over an inverted index.

Pure local infrastructure: Python + NumPy only, no external models or data.
"""

from sparse_retrieval.vector import SparseVector
from sparse_retrieval.index import InvertedIndex, QueryResult
from sparse_retrieval.brute_force import brute_force_query

__all__ = ["SparseVector", "InvertedIndex", "QueryResult", "brute_force_query"]

__version__ = "0.1.0"
