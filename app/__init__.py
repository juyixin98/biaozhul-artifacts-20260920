"""Equity Penalty Evidence Aggregation (权益惩罚证据归并).

Pure-backend service that ingests offline votes carrying test signatures,
detects validator double-signing evidence, and applies stake-slashing penalties
exactly once against the stake snapshot frozen at the epoch boundary.
"""
