"""Swappable learners over execution observations.

Core Tessera never imports this package. Learners use only the public client
contract, write ordinary versioned nouns and run as ordinary verbs. See
docs/execution-learning.md.
"""

from tessera.learners.catalog import SPECS, spec
from tessera.learners.contract import (
    Activation,
    LearnerConfig,
    LearnerSpec,
    NounRef,
    RunRequest,
    run_spec,
    value_digest,
)

__all__ = [
    "SPECS",
    "Activation",
    "LearnerConfig",
    "LearnerSpec",
    "NounRef",
    "RunRequest",
    "run_spec",
    "spec",
    "value_digest",
]
