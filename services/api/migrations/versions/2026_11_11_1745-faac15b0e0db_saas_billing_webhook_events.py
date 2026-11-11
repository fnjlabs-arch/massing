"""SaaS billing (SadSha Build Pro §2): webhook idempotency + past-due grace clock.

- New table `billing_webhook_events` (one row per processed provider delivery; the unique
  index on `event_key` is what makes a retried Lemon Squeezy delivery a no-op instead of a
  double-apply — see billing.apply_subscription_event).
- New nullable column `organizations.past_due_since`: when the org entered past_due, so the
  7-day grace in billing.refresh_org_status has a clock to run from. Nullable so the additive
  runtime sync (`db._ensure_columns`) backfills existing DBs.

Revision ID: faac15b0e0db
Revises: 1d637b6881a9
Create Date: 2026-11-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'faac15b0e0db'
down_revision: str | None = '1d637b6881a9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('billing_webhook_events',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('event_key', sa.String(), nullable=False),
    sa.Column('event_name', sa.String(), nullable=False),
    sa.Column('org_id', sa.String(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('billing_webhook_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_billing_webhook_events_event_key'), ['event_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_billing_webhook_events_org_id'), ['org_id'], unique=False)
        batch_op.create_index('uq_billing_webhook_events_key', ['event_key'], unique=True)

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('past_due_since', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_column('past_due_since')

    with op.batch_alter_table('billing_webhook_events', schema=None) as batch_op:
        batch_op.drop_index('uq_billing_webhook_events_key')
        batch_op.drop_index(batch_op.f('ix_billing_webhook_events_org_id'))
        batch_op.drop_index(batch_op.f('ix_billing_webhook_events_event_key'))

    op.drop_table('billing_webhook_events')
