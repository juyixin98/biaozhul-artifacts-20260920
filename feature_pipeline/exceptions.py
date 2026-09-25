"""Typed errors for the feature pipeline. All failures fail fast with clear messages."""


class FeaturePipelineError(Exception):
    """Base class for every pipeline-related error."""


class SchemaError(FeaturePipelineError):
    """Input data does not match the declared column schema."""


class FittingError(FeaturePipelineError):
    """A transformer cannot be fitted on the given column."""


class NotFittedError(FeaturePipelineError):
    """transform() was called before fit()."""


class SerializationError(FeaturePipelineError):
    """The serialized pipeline artifact is missing or invalid."""
