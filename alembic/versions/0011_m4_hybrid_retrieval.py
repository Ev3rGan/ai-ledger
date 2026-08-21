"""Add versioned pgvector accepted-knowledge retrieval indexes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "retrieval_indexes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.String(length=255), nullable=False),
        sa.Column("profile_sha256", sa.String(length=64), nullable=False),
        sa.Column("profile_definition", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("documents_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embeddings_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fault_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "state IN ('building', 'active', 'retired', 'failed')",
            name="ck_retrieval_indexes_state",
        ),
        sa.CheckConstraint(
            "documents_indexed >= 0 AND chunks_indexed >= 0 AND embeddings_indexed >= 0",
            name="ck_retrieval_indexes_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "length(profile_sha256) = 64",
            name="ck_retrieval_indexes_profile_hash_length",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_retrieval_indexes_single_active",
        "retrieval_indexes",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "retrieval_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "search_vector",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_retrieval_chunks_offsets",
        ),
        sa.CheckConstraint("token_count >= 1", name="ck_retrieval_chunks_token_count"),
        sa.CheckConstraint(
            "length(text_hash) = 64",
            name="ck_retrieval_chunks_text_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["retrieval_indexes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "index_id",
            "document_version_id",
            "ordinal",
            name="uq_retrieval_chunks_index_document_ordinal",
        ),
    )
    op.create_index(
        "ix_retrieval_chunks_search_vector",
        "retrieval_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_retrieval_chunks_index_id",
        "retrieval_chunks",
        ["index_id"],
    )
    op.create_table(
        "retrieval_chunk_entities",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["retrieval_chunks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", "normalized_name"),
    )
    op.create_index(
        "ix_retrieval_chunk_entities_normalized_name",
        "retrieval_chunk_entities",
        ["normalized_name"],
    )
    op.create_table(
        "retrieval_runtime_states",
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("index_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=True),
        sa.Column("revision", sa.String(length=64), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("fault_code", sa.String(length=64), nullable=True),
        sa.Column("fault_detail", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('index', 'embedding', 'reranker')",
            name="ck_retrieval_runtime_states_stage",
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'degraded', 'unavailable')",
            name="ck_retrieval_runtime_states_state",
        ),
        sa.ForeignKeyConstraint(["index_id"], ["retrieval_indexes.id"]),
        sa.PrimaryKeyConstraint("stage"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM retrieval_indexes) THEN
                RAISE EXCEPTION
                    '0011 retrieval index data exists; this migration cannot be downgraded';
            END IF;
        END;
        $$
        """
    )
    op.drop_table("retrieval_runtime_states")
    op.drop_index(
        "ix_retrieval_chunk_entities_normalized_name",
        table_name="retrieval_chunk_entities",
    )
    op.drop_table("retrieval_chunk_entities")
    op.drop_index("ix_retrieval_chunks_index_id", table_name="retrieval_chunks")
    op.drop_index("ix_retrieval_chunks_search_vector", table_name="retrieval_chunks")
    op.drop_table("retrieval_chunks")
    op.drop_index("uq_retrieval_indexes_single_active", table_name="retrieval_indexes")
    op.drop_table("retrieval_indexes")
