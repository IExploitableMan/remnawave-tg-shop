import logging
from pathlib import Path
from typing import Tuple

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from config.settings import Settings


import os
_BASELINE_REVISION = "0001_initial_schema"


def _build_alembic_config(settings: Settings) -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _inspect_database_state(connection: Connection) -> Tuple[bool, bool, bool]:
    db_inspector = inspect(connection)
    has_alembic_version = db_inspector.has_table("alembic_version")
    has_users_table = db_inspector.has_table("users")
    has_legacy_migrator_table = db_inspector.has_table("schema_migrations")
    return has_alembic_version, has_users_table, has_legacy_migrator_table


def _run_legacy_migrator_compatibility(connection: Connection) -> None:
    db_inspector = inspect(connection)
    if not db_inspector.has_table("users"):
        return

    users_columns = {
        column["name"]
        for column in db_inspector.get_columns("users")
    }
    user_alter_statements = []

    if "channel_subscription_verified" not in users_columns:
        user_alter_statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_verified BOOLEAN"
        )
    if "channel_subscription_checked_at" not in users_columns:
        user_alter_statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_checked_at TIMESTAMPTZ"
        )
    if "channel_subscription_verified_for" not in users_columns:
        user_alter_statements.append(
            "ALTER TABLE users ADD COLUMN channel_subscription_verified_for BIGINT"
        )
    if "referral_code" not in users_columns:
        user_alter_statements.append(
            "ALTER TABLE users ADD COLUMN referral_code VARCHAR(16)"
        )

    for statement in user_alter_statements:
        connection.execute(text(statement))

    users_columns = {
        column["name"]
        for column in inspect(connection).get_columns("users")
    }
    if "referral_code" in users_columns:
        connection.execute(
            text(
                """
                UPDATE users
                SET referral_code = NULLIF(UPPER(BTRIM(referral_code)), '')
                WHERE referral_code IS NOT NULL
                """
            )
        )
        connection.execute(
            text(
                """
                WITH duplicate_codes AS (
                    SELECT
                        user_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY referral_code
                            ORDER BY user_id
                        ) AS rn
                    FROM users
                    WHERE referral_code IS NOT NULL
                )
                UPDATE users AS u
                SET referral_code = UPPER(
                    SUBSTRING(
                        md5(
                            u.user_id::text
                            || clock_timestamp()::text
                            || random()::text
                        )
                        FROM 1 FOR 9
                    )
                )
                FROM duplicate_codes AS d
                WHERE u.user_id = d.user_id
                  AND d.rn > 1
                """
            )
        )
        connection.execute(
            text(
                """
                WITH generated_codes AS (
                    SELECT
                        user_id,
                        UPPER(
                            SUBSTRING(
                                md5(
                                    user_id::text
                                    || clock_timestamp()::text
                                    || random()::text
                                )
                                FROM 1 FOR 9
                            )
                        ) AS referral_code
                    FROM users
                    WHERE referral_code IS NULL
                )
                UPDATE users AS u
                SET referral_code = g.referral_code
                FROM generated_codes AS g
                WHERE u.user_id = g.user_id
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code
                ON users (referral_code)
                WHERE referral_code IS NOT NULL
                """
            )
        )

    db_inspector = inspect(connection)
    if db_inspector.has_table("payments"):
        payments_columns = {
            column["name"]
            for column in db_inspector.get_columns("payments")
        }
        if "original_amount" not in payments_columns:
            connection.execute(text("ALTER TABLE payments ADD COLUMN original_amount FLOAT"))
        if "discount_applied" not in payments_columns:
            connection.execute(text("ALTER TABLE payments ADD COLUMN discount_applied FLOAT"))

    db_inspector = inspect(connection)
    has_promo_codes = db_inspector.has_table("promo_codes")
    if has_promo_codes:
        promo_columns = {
            column["name"]
            for column in db_inspector.get_columns("promo_codes")
        }
        if "promo_type" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN promo_type VARCHAR NOT NULL DEFAULT 'bonus_days'"
                )
            )
        if "discount_percentage" not in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ADD COLUMN discount_percentage INTEGER")
            )
        if "current_activations" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN current_activations INTEGER NOT NULL DEFAULT 0"
                )
            )
        else:
            connection.execute(
                text(
                    "UPDATE promo_codes SET current_activations = 0 "
                    "WHERE current_activations IS NULL"
                )
            )
            connection.execute(
                text("ALTER TABLE promo_codes ALTER COLUMN current_activations SET DEFAULT 0")
            )
            connection.execute(
                text("ALTER TABLE promo_codes ALTER COLUMN current_activations SET NOT NULL")
            )
        if "bonus_days" in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ALTER COLUMN bonus_days DROP NOT NULL")
            )
        if "max_discount_amount" not in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ADD COLUMN max_discount_amount FLOAT")
            )
        if "traffic_amount_gb" not in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ADD COLUMN traffic_amount_gb FLOAT")
            )
        if "min_user_registration_date" not in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ADD COLUMN min_user_registration_date TIMESTAMPTZ")
            )
        if "registration_date_direction" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN registration_date_direction VARCHAR NOT NULL DEFAULT 'after'"
                )
            )
        if "renewal_only" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN renewal_only BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "subscription_presence_mode" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN subscription_presence_mode VARCHAR NOT NULL DEFAULT 'any'"
                )
            )
        if "applies_to_combined_subscription" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN applies_to_combined_subscription BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "combined_discount_scope" not in promo_columns:
            connection.execute(
                text(
                    "ALTER TABLE promo_codes ADD COLUMN combined_discount_scope VARCHAR NOT NULL DEFAULT 'base_only'"
                )
            )
        if "last_activated_at" not in promo_columns:
            connection.execute(
                text("ALTER TABLE promo_codes ADD COLUMN last_activated_at TIMESTAMPTZ")
            )
        connection.execute(
            text(
                "UPDATE promo_codes SET subscription_presence_mode = 'active_only' "
                "WHERE renewal_only = TRUE AND subscription_presence_mode = 'any'"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_promo_codes_promo_type ON promo_codes (promo_type)"
            )
        )

    db_inspector = inspect(connection)
    has_active_discounts = db_inspector.has_table("active_discounts")
    if not has_active_discounts and has_promo_codes:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS active_discounts (
                    user_id BIGINT PRIMARY KEY,
                    promo_code_id INTEGER NOT NULL,
                    discount_percentage INTEGER NOT NULL,
                    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fk_active_discounts_user
                        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
                    CONSTRAINT fk_active_discounts_promo_code
                        FOREIGN KEY (promo_code_id) REFERENCES promo_codes (promo_code_id) ON DELETE CASCADE
                )
                """
            )
        )

    db_inspector = inspect(connection)
    if db_inspector.has_table("users") and not db_inspector.has_table("server_reports"):
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS server_reports (
                    report_id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id),
                    issue_type VARCHAR NOT NULL,
                    details TEXT,
                    status VARCHAR NOT NULL DEFAULT 'new',
                    created_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_reports_user_id ON server_reports (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_reports_issue_type ON server_reports (issue_type)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_reports_status ON server_reports (status)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_reports_created_at ON server_reports (created_at)"))

    db_inspector = inspect(connection)
    if db_inspector.has_table("server_reports"):
        columns = {column["name"] for column in db_inspector.get_columns("server_reports")}
        if "details" not in columns:
            connection.execute(text("ALTER TABLE server_reports ADD COLUMN details TEXT"))

    db_inspector = inspect(connection)
    if db_inspector.has_table("server_reports") and not db_inspector.has_table("server_report_hosts"):
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS server_report_hosts (
                    report_host_id SERIAL PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES server_reports(report_id) ON DELETE CASCADE,
                    host_uuid VARCHAR NOT NULL,
                    host_name VARCHAR NOT NULL,
                    host_address VARCHAR,
                    node_uuid VARCHAR,
                    node_name VARCHAR,
                    profile_kind VARCHAR
                )
                """
            )
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_report_hosts_report_id ON server_report_hosts (report_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_report_hosts_host_uuid ON server_report_hosts (host_uuid)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_server_report_hosts_node_uuid ON server_report_hosts (node_uuid)"))

    db_inspector = inspect(connection)
    if not db_inspector.has_table("admin_server_report_preferences"):
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS admin_server_report_preferences (
                    admin_id BIGINT PRIMARY KEY,
                    reports_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ
                )
                """
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_admin_server_report_preferences_reports_enabled "
                "ON admin_server_report_preferences (reports_enabled)"
            )
        )
        has_active_discounts = True

    if has_active_discounts and has_promo_codes:
        connection.execute(
            text(
                "DELETE FROM active_discounts ad "
                "WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = ad.user_id) "
                "OR NOT EXISTS (SELECT 1 FROM promo_codes p WHERE p.promo_code_id = ad.promo_code_id)"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "DROP CONSTRAINT IF EXISTS active_discounts_user_id_fkey"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "DROP CONSTRAINT IF EXISTS fk_active_discounts_user"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "DROP CONSTRAINT IF EXISTS active_discounts_promo_code_id_fkey"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "DROP CONSTRAINT IF EXISTS fk_active_discounts_promo_code"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "ADD CONSTRAINT fk_active_discounts_user "
                "FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE active_discounts "
                "ADD CONSTRAINT fk_active_discounts_promo_code "
                "FOREIGN KEY (promo_code_id) REFERENCES promo_codes (promo_code_id) ON DELETE CASCADE"
            )
        )
    elif has_active_discounts and not has_promo_codes:
        logging.warning(
            "Alembic legacy compatibility: skipped active_discounts FK repair "
            "because promo_codes table is missing."
        )


def _run_stamp(connection: Connection, alembic_config: Config, revision: str) -> None:
    alembic_config.attributes["connection"] = connection
    command.stamp(alembic_config, revision)


def _run_upgrade(connection: Connection, alembic_config: Config) -> None:
    alembic_config.attributes["connection"] = connection
    command.upgrade(alembic_config, "head")


async def run_alembic_migrations(settings: Settings, async_engine: AsyncEngine) -> None:
    """Apply Alembic migrations with bootstrap for existing installations."""

    alembic_config = _build_alembic_config(settings)
    allow_bootstrap_without_legacy = os.getenv("ALEMBIC_ALLOW_STAMP_WITHOUT_LEGACY", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    async with async_engine.begin() as async_connection:
        (
            has_alembic_version,
            has_users_table,
            has_legacy_migrator_table,
        ) = await async_connection.run_sync(
            _inspect_database_state
        )

        if not has_alembic_version and has_users_table:
            if not has_legacy_migrator_table and not allow_bootstrap_without_legacy:
                raise RuntimeError(
                    "Alembic bootstrap refused: found existing users table without "
                    "alembic_version and without legacy schema_migrations marker. "
                    "Set ALEMBIC_ALLOW_STAMP_WITHOUT_LEGACY=true to explicitly allow "
                    f"stamping baseline {_BASELINE_REVISION} after manual verification."
                )

            if has_legacy_migrator_table:
                logging.info(
                    "Alembic: applying legacy migrator compatibility fixes before stamp."
                )
                await async_connection.run_sync(_run_legacy_migrator_compatibility)
            else:
                logging.warning(
                    "Alembic: existing users table without legacy schema_migrations; "
                    "proceeding with explicit bootstrap override flag."
                )

            logging.info(
                "Alembic: existing schema detected without alembic_version; stamping %s.",
                _BASELINE_REVISION,
            )
            await async_connection.run_sync(
                _run_stamp,
                alembic_config,
                _BASELINE_REVISION,
            )

        logging.info("Alembic: running upgrade to head...")
        await async_connection.run_sync(_run_upgrade, alembic_config)

    logging.info("Alembic: migrations applied successfully.")
