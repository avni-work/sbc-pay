"""payment_transaction_changes

Revision ID: 170ee3d24bcf
Revises: 8432e683d12c
Create Date: 2019-05-27 10:51:44.851868

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '170ee3d24bcf'
down_revision = '8432e683d12c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('payment_transaction', 'transaction_end_time',
               existing_type=postgresql.TIMESTAMP(),
               nullable=True)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('payment_transaction', 'transaction_end_time',
               existing_type=postgresql.TIMESTAMP(),
               nullable=False)
    # ### end Alembic commands ###
