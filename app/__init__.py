"""Offline trajectory error evaluation API.

Pure backend: estimates/ground-truth poses are associated by a bounded
time difference, rigidly (and optionally similarity) aligned, then scored
with ATE and fixed-span RPE.
"""
