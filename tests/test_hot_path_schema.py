"""Metadata checks for the direct-source hot-path schema."""

import unittest

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint
from sqlalchemy.sql import FromClause

from ceho.models import MediaObject, OutboxEvent, SessionUser, Transcript, User


class HotPathSchemaTest(unittest.TestCase):
    """Verify membership, media provenance, and outbox constraints on the models."""

    def test_users_store_external_ids_as_json_object_with_gin_index(self) -> None:
        users = _as_table(User.__table__)
        index_names = {index.name for index in users.indexes}

        self.assertNotIn("external_id", users.c)
        self.assertIn("external_ids", users.c)
        self.assertFalse(users.c.external_ids.nullable)
        self.assertIn("users_external_ids_object_check", _check_names(users))
        self.assertIn("users_external_ids_gin_idx", index_names)

    def test_known_user_membership_is_unique_and_anonymous_user_stays_nullable(self) -> None:
        constraint = _unique(
            _as_table(SessionUser.__table__),
            "session_users_tenant_session_user_unique",
        )

        self.assertEqual(
            [column.name for column in constraint.columns],
            ["tenant_id", "session_id", "user_id"],
        )
        self.assertTrue(SessionUser.__table__.c.user_id.nullable)

    def test_media_object_is_tenant_and_session_scoped(self) -> None:
        foreign_key = _foreign_key(
            _as_table(MediaObject.__table__),
            "media_objects_tenant_session_fkey",
        )

        self.assertEqual(
            [column.name for column in foreign_key.columns],
            ["tenant_id", "session_id"],
        )
        self.assertEqual(foreign_key.ondelete, "CASCADE")
        self.assertFalse(MediaObject.__table__.c.storage_uri.nullable)
        media = _as_table(MediaObject.__table__)
        self.assertIn("media_objects_complete_write_check", _check_names(media))
        self.assertIn("media_objects_deleted_at_check", _check_names(media))

    def test_transcript_media_reference_is_optional_and_session_scoped(self) -> None:
        foreign_key = _foreign_key(_as_table(Transcript.__table__), "transcripts_media_object_fkey")

        self.assertEqual(
            [column.name for column in foreign_key.columns],
            ["tenant_id", "session_id", "media_object_id"],
        )
        self.assertEqual(foreign_key.ondelete, "RESTRICT")
        self.assertTrue(Transcript.__table__.c.media_object_id.nullable)
        self.assertTrue(Transcript.__table__.c.media_start_offset_ms.nullable)
        self.assertTrue(Transcript.__table__.c.media_end_offset_ms.nullable)
        self.assertIn(
            "transcripts_media_provenance_check",
            _check_names(_as_table(Transcript.__table__)),
        )

    def test_outbox_event_can_share_a_session_transaction(self) -> None:
        outbox = _as_table(OutboxEvent.__table__)
        foreign_key = _foreign_key(outbox, "outbox_events_tenant_session_fkey")
        sequence = _unique(outbox, "outbox_events_session_sequence_unique")

        self.assertEqual(
            [column.name for column in foreign_key.columns],
            ["tenant_id", "session_id"],
        )
        self.assertEqual(
            [column.name for column in sequence.columns],
            ["tenant_id", "session_id", "sequence"],
        )
        self.assertFalse(OutboxEvent.__table__.c.payload.nullable)
        self.assertIn("outbox_events_type_check", _check_names(outbox))
        self.assertIn("outbox_events_published_at_check", _check_names(outbox))


def _as_table(clause: FromClause) -> Table:
    if not isinstance(clause, Table):
        raise AssertionError("mapped class has no table")
    return clause


def _unique(table: Table, name: str) -> UniqueConstraint:
    found = next(
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name == name
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"{name} is missing")
    return found


def _foreign_key(table: Table, name: str) -> ForeignKeyConstraint:
    found = next(
        (
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"{name} is missing")
    return found


def _check_names(table: Table) -> set[str]:
    names: set[str] = set()
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str):
            names.add(constraint.name)
    return names
