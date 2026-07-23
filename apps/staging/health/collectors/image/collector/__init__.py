"""Collector package root.

This module stays import-light on purpose: tests must be able to import package metadata
without pulling optional runtime dependencies such as database drivers or API clients.
"""

__version__ = "0.1.0"
