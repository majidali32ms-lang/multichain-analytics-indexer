"""Exceptions raised by the library. Catch ``IndexerError`` to handle them all."""

from __future__ import annotations


class IndexerError(Exception):
    """Base class for every failure a caller is expected to handle."""


class ConfigError(IndexerError):
    """A setting is missing or has an invalid value."""


class InvalidInput(IndexerError, ValueError):
    """A value supplied by the caller failed validation."""


class InvalidAddress(InvalidInput):
    """An address or account ID is not valid for the chain."""


class UnsupportedChain(IndexerError):
    """The chain is unknown, or not available in this version."""


class NotFound(IndexerError):
    """The node has no record of the requested item."""


class MalformedResponse(IndexerError):
    """The node answered with data that does not match its documented format."""


class WrongNetwork(IndexerError):
    """The configured endpoint serves a different network than the one requested."""


class NetworkError(IndexerError):
    """The node could not be reached, or kept failing after every retry."""


class RateLimited(NetworkError):
    """The node kept refusing requests with HTTP 429 after every retry."""
