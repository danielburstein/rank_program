"""uuid_ranks: one row per resolved UUID with its highest rank

Revision ID: b7c1d2e3f4a5
Revises: 5976d3fba653
Create Date: 2026-11-11 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b7c1d2e3f4a5'
down_revision = '5976d3fba653'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'uuid_ranks',
        sa.Column('uuid', sa.String(length=36), nullable=False),
        sa.Column('current_name', sa.String(length=64), nullable=True),
        sa.Column('highest_rank', sa.String(length=32), nullable=False),
        sa.Column('rank_priority', sa.Integer(), nullable=False),
        sa.Column('new_rank', sa.String(length=64), nullable=True),
        sa.Column('usernames', sa.JSON(), nullable=False),
        sa.Column('player_ids', sa.JSON(), nullable=False),
        sa.Column('ranks_seen', sa.JSON(), nullable=False),
        sa.Column('identity_sources', sa.JSON(), nullable=False),
        sa.Column('is_bedrock', sa.Boolean(), nullable=False),
        sa.Column('records', sa.Integer(), nullable=False),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('uuid'),
    )
    with op.batch_alter_table('uuid_ranks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_uuid_ranks_current_name'), ['current_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_uuid_ranks_highest_rank'), ['highest_rank'], unique=False)
        batch_op.create_index(batch_op.f('ix_uuid_ranks_new_rank'), ['new_rank'], unique=False)
        batch_op.create_index(batch_op.f('ix_uuid_ranks_rank_priority'), ['rank_priority'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('uuid_ranks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_uuid_ranks_rank_priority'))
        batch_op.drop_index(batch_op.f('ix_uuid_ranks_new_rank'))
        batch_op.drop_index(batch_op.f('ix_uuid_ranks_highest_rank'))
        batch_op.drop_index(batch_op.f('ix_uuid_ranks_current_name'))
    op.drop_table('uuid_ranks')
