"""Benchmark suite for the prospect-agent.

Two complementary harnesses:
- coverage.py:  measures hit-rate per field on random Sirene companies.
                No ground truth needed. Answers "does the agent find the info?".
- precision.py: measures correctness per field vs a golden-truth CSV.
                Answers "is the info actually right?".
"""
