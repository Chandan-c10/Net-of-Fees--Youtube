"""Typed failures.

Every stage fails loudly with one of these. A stage never emits a degraded
artifact and continues -- see the architecture doc, section 5.
"""


class PipelineError(Exception):
    """Base for every failure this pipeline raises deliberately."""

    exit_code = 1


class ConfigError(PipelineError):
    """Configuration is missing or contradictory."""

    exit_code = 2


class ManifestError(PipelineError):
    """The manifest is absent, malformed, or disagrees with itself."""

    exit_code = 3


class StageError(PipelineError):
    """A stage could not do its job."""

    exit_code = 4


class GateError(PipelineError):
    """An operation was attempted across a gate that has not been cleared."""

    exit_code = 5


class ScrubError(PipelineError):
    """A secret was found in episode material. Hard stop."""

    exit_code = 6


class ValidationError(PipelineError):
    """Script or scene content is well-formed but invalid."""

    exit_code = 7
