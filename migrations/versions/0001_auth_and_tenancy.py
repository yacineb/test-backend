"""Organizations, users, refresh tokens, and row-level tenant isolation.

Revision ID: 0001_auth_tenancy
Revises:
Create Date: 2026-07-26

"""

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_auth_tenancy"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tenant-scoped tables and the column each one keys its policy on.
_TENANT_TABLES = {
    "organizations": "id",
    "users": "org_id",
    "refresh_tokens": "org_id",
}

# NULLIF so that an unset *or empty* setting yields NULL rather than a cast
# error, and the policy predicate evaluates false. Default deny.
_CURRENT_ORG = "(NULLIF(current_setting('app.current_org_id', true), ''))::uuid"

_SAFE_PASSWORD = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _role_password(env_var: str, default: str) -> str:
    """Read a role password, restricted to characters that need no escaping.

    DO blocks cannot take bind parameters, so this value is interpolated into
    SQL. The charset check is what makes that safe.
    """
    value = os.environ.get(env_var, default)
    if not _SAFE_PASSWORD.fullmatch(value):
        raise RuntimeError(
            f"{env_var} must match [A-Za-z0-9_.-] and be 1-64 characters long"
        )
    return value


def _create_role(name: str, password: str, *, bypass_rls: bool) -> None:
    attributes = "LOGIN" + (" BYPASSRLS" if bypass_rls else " NOBYPASSRLS")
    op.execute(
        sa.text(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                CREATE ROLE {name} {attributes} PASSWORD '{password}';
            ELSE
                ALTER ROLE {name} {attributes} PASSWORD '{password}';
            END IF;
        END
        $$;
        """)
    )


def upgrade() -> None:
    # app_rw: the application role. Subject to RLS, so a query that loses its
    # WHERE clause returns nothing instead of another tenant's rows.
    _create_role(
        "app_rw", _role_password("APP_RW_PASSWORD", "app_rw"), bypass_rls=False
    )
    # app_auth: BYPASSRLS, used only for lookups that precede authentication
    # (user by email, refresh token by hash) and for seeding.
    _create_role(
        "app_auth", _role_password("APP_AUTH_PASSWORD", "app_auth"), bypass_rls=True
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_users_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_refresh_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_refresh_tokens_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.execute("GRANT USAGE ON SCHEMA public TO app_rw, app_auth")

    for table, column in _TENANT_TABLES.items():
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_rw, app_auth"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the table owner is constrained too, should ownership ever
        # move to a non-superuser role.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY org_isolation ON {table}
                FOR ALL
                USING ({column} = {_CURRENT_ORG})
                WITH CHECK ({column} = {_CURRENT_ORG})
        """)


def downgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")

    op.execute("REVOKE USAGE ON SCHEMA public FROM app_rw, app_auth")
