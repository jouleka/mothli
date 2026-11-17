"""Strongly typed identifiers crossing the hosted tenant boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OwnerId:
    value: uuid.UUID


class MissingTenantResourceError(LookupError):
    pass


class StalePositionEpochError(RuntimeError):
    pass


class FuturePositionVersionError(RuntimeError):
    pass


class InvalidPositionError(ValueError):
    pass


class AlreadyOnShelfError(Exception):
    """The reader already has a live book added from this free-library entry."""

    def __init__(self, book_id: uuid.UUID) -> None:
        super().__init__("already on the shelf")
        self.book_id = book_id


@dataclass(frozen=True, slots=True)
class SourceObjectRecord:
    book_id: uuid.UUID
    book_incarnation: uuid.UUID
    object_id: uuid.UUID
    provider: str
    media_type: str
    byte_size: int
    sha256: str
    encryption: str
