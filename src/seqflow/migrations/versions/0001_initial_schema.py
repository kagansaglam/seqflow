"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-03 20:24:02.207019
"""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=128), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runs_run_id'), ['run_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_runs_status'), ['status'], unique=False)

    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_run_id'), ['run_id'], unique=False)

    op.create_table('pipeline_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('command', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pipeline_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pipeline_jobs_run_id'), ['run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_pipeline_jobs_status'), ['status'], unique=False)

    op.create_table('qc_metrics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('sample_id', sa.String(length=128), nullable=True),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('qc_metrics', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_qc_metrics_run_id'), ['run_id'], unique=False)

    op.create_table('samples',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('sample_id', sa.String(length=128), nullable=False),
    sa.Column('project', sa.String(length=128), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'sample_id')
    )
    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_samples_run_id'), ['run_id'], unique=False)

    op.create_table('libraries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sample_pk', sa.Integer(), nullable=False),
    sa.Column('index1', sa.String(length=32), nullable=True),
    sa.Column('index2', sa.String(length=32), nullable=True),
    sa.Column('lane', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['sample_pk'], ['samples.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('libraries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_libraries_sample_pk'), ['sample_pk'], unique=False)

    op.create_table('fastq_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('library_id', sa.Integer(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('read', sa.String(length=8), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('md5', sa.String(length=32), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['library_id'], ['libraries.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('path')
    )
    with op.batch_alter_table('fastq_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fastq_files_library_id'), ['library_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('fastq_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fastq_files_library_id'))

    op.drop_table('fastq_files')
    with op.batch_alter_table('libraries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_libraries_sample_pk'))

    op.drop_table('libraries')
    with op.batch_alter_table('samples', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_samples_run_id'))

    op.drop_table('samples')
    with op.batch_alter_table('qc_metrics', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_qc_metrics_run_id'))

    op.drop_table('qc_metrics')
    with op.batch_alter_table('pipeline_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pipeline_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_pipeline_jobs_run_id'))

    op.drop_table('pipeline_jobs')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_run_id'))

    op.drop_table('events')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_status'))
        batch_op.drop_index(batch_op.f('ix_runs_run_id'))

    op.drop_table('runs')
