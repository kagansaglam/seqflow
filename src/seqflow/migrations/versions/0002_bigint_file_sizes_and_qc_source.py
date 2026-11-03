"""bigint file sizes and qc source

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03 20:24:17.324484
"""
import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('fastq_files', schema=None) as batch_op:
        batch_op.alter_column('size_bytes',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=True)

    with op.batch_alter_table('qc_metrics', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=32), server_default='manual', nullable=False))
        batch_op.create_index('ix_qc_metrics_run_name', ['run_id', 'name'], unique=False)
        batch_op.create_unique_constraint('uq_qc_metric', ['run_id', 'sample_id', 'name', 'source'])

    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_samples_project'), ['project'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_samples_project'))

    with op.batch_alter_table('qc_metrics', schema=None) as batch_op:
        batch_op.drop_constraint('uq_qc_metric', type_='unique')
        batch_op.drop_index('ix_qc_metrics_run_name')
        batch_op.drop_column('source')

    with op.batch_alter_table('fastq_files', schema=None) as batch_op:
        batch_op.alter_column('size_bytes',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=True)

