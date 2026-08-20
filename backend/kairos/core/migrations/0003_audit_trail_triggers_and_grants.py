"""The audit trail's actual enforcement mechanism (RFC v1.0 §12; Spec
v1.0 §3): a dedicated, least-privilege application role, and triggers that
fire on the write itself rather than on any particular code path calling
it. 0002_auditlog only created the table these write to.

Why a role AND triggers, not one or the other: the triggers make auditing
mandatory (AUD-02 — a raw SQL write bypassing the service layer entirely
still produces a row, because nothing about the trigger depends on how the
row got written). The role's grants make the audit APPEND-ONLY mandatory
in the same sense (AUD-01 — proven by connecting AS that role and watching
UPDATE/DELETE fail with insufficient privilege, not by trusting that no
code path happens to attempt one). Each closes a different bypass.
"""

from __future__ import annotations

from django.db import migrations

# ============================================================
# Verbatim from Spec v1.0 §3 — reproduced at the point of definition for
# the same reason bookings/0002_exclusion_constraint.py reproduces the
# EXCLUDE constraint's comment block (Implementation Plan §1.3's "load-
# bearing comment sites"): if you are reading this while modifying or
# dropping the kairos_app role's grants, or the triggers below, during an
# unrelated migration — STOP. The append-only guarantee (PRD FR41) is
# enforced HERE, at the grant level, not by any application code path
# choosing to refrain from UPDATE/DELETE. Weakening these grants silently
# reintroduces a mutable audit trail, and no test outside AUD-01 will fail.
# ============================================================
GRANTS_SQL = """
-- Roles are CLUSTER-WIDE in Postgres, not per-database — this migration
-- reruns harmlessly against a freshly created pytest test database on the
-- same cluster (the role already exists from an earlier run against
-- kairos_dev or a prior test run) as well as the very first time, since
-- plain CREATE ROLE has no IF NOT EXISTS form.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'kairos_app') THEN
        -- Dev-only literal credential, matching infra/docker-compose.yml's
        -- own hardcoded POSTGRES_PASSWORD precedent. Rollout (Phase 30)
        -- must replace this with a real managed secret before any
        -- deployment; nothing here is meant to survive past local dev.
        CREATE ROLE kairos_app LOGIN PASSWORD 'kairos_app';
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO kairos_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO kairos_app;

-- Django's OWN bookkeeping table, not an application table — every
-- management command (runserver included) queries this at startup via
-- check_migrations() before serving a single request. Read-only: the app
-- never writes it, only `manage.py migrate` does, which runs elevated.
-- Missing this grant doesn't fail a specific endpoint — it fails the
-- application from starting at all, caught empirically by actually
-- running `manage.py runserver` under kairos_app rather than only
-- exercising the ORM through management-command-free test paths.
GRANT SELECT ON django_migrations TO kairos_app;

-- Ordinary application tables: full DML. The app both reads and writes
-- these as part of normal operation.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON app_user, resource, resource_admin, booking, idempotency_key
    TO kairos_app;

-- Sequence backing this schema's one BigAutoField primary key
-- (audit_log.id — every other table, including resource_admin as of
-- identity/0003, uses an application-generated UUID, no sequence
-- involved). Granted broadly on every sequence in the schema, not
-- enumerated individually, so a future phase adding another BigAutoField/
-- AutoField PK doesn't silently need a second migration just to grant
-- nextval() access before its first write can succeed.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kairos_app;

-- APPEND-ONLY IS ENFORCED AT THE GRANT LEVEL, NOT IN APPLICATION CODE
-- (PRD FR41). kairos_app holds INSERT and SELECT only on audit_log — no
-- API path can modify history because no API path *can*. The explicit
-- REVOKE is not redundant with simply never granting UPDATE/DELETE: it
-- documents the guarantee at the point someone would look for it, and
-- stays correct even if a future, broader GRANT statement is added above
-- without this file being revisited.
GRANT SELECT, INSERT ON audit_log TO kairos_app;
REVOKE UPDATE, DELETE ON audit_log FROM kairos_app;
"""

REVERSE_GRANTS_SQL = """
REVOKE ALL ON app_user, resource, resource_admin, booking, idempotency_key, audit_log,
    django_migrations
    FROM kairos_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM kairos_app;
REVOKE USAGE ON SCHEMA public FROM kairos_app;
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM kairos_app', current_database());
END
$$;
"""

# Triggers cannot see the authenticated principal — the service layer sets
# these transaction-local session variables at the start of every write
# transaction, via the SAME `apply_write_path_session_settings` call that
# also sets the write-path timeouts (kairos/core/db.py), not a second,
# separate mechanism:
#   SELECT set_config('app.actor_id',   '<uuid>', true);
#   SELECT set_config('app.actor_type', 'user' | 'admin' | 'system', true);
#   SELECT set_config('app.reason',     '<text>', true);  -- '' if absent
#   SELECT set_config('app.request_id', '<text>', true);
TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION write_audit_log() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_log (
        entity_type, entity_id, action, actor_id, actor_type, reason, request_id,
        before_state, after_state
    ) VALUES (
        TG_TABLE_NAME,
        COALESCE(NEW.id, OLD.id),
        LOWER(TG_OP),
        NULLIF(current_setting('app.actor_id', true), '')::UUID,
        COALESCE(NULLIF(current_setting('app.actor_type', true), ''), 'unknown'),
        NULLIF(current_setting('app.reason', true), ''),
        NULLIF(current_setting('app.request_id', true), ''),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- waitlist_entry and waitlist_offer triggers are added in Phases 14/16,
-- when those tables exist — not stubbed here against a table that doesn't
-- exist yet.
CREATE TRIGGER audit_booking        AFTER INSERT OR UPDATE OR DELETE ON booking
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_resource       AFTER INSERT OR UPDATE OR DELETE ON resource
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
CREATE TRIGGER audit_resource_admin AFTER INSERT OR UPDATE OR DELETE ON resource_admin
    FOR EACH ROW EXECUTE FUNCTION write_audit_log();
"""

REVERSE_TRIGGER_FUNCTION_SQL = """
DROP TRIGGER IF EXISTS audit_booking        ON booking;
DROP TRIGGER IF EXISTS audit_resource       ON resource;
DROP TRIGGER IF EXISTS audit_resource_admin ON resource_admin;
DROP FUNCTION IF EXISTS write_audit_log();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_auditlog"),
        ("bookings", "0002_exclusion_constraint"),
        ("resources", "0001_initial"),
        # 0003, not 0002 — resource_admin's `id` must already be UUID
        # before this migration's trigger is attached to that table (see
        # identity/0003_alter_resourceadmin_id.py).
        ("identity", "0003_alter_resourceadmin_id"),
    ]

    operations = [
        migrations.RunSQL(sql=GRANTS_SQL, reverse_sql=REVERSE_GRANTS_SQL),
        migrations.RunSQL(sql=TRIGGER_FUNCTION_SQL, reverse_sql=REVERSE_TRIGGER_FUNCTION_SQL),
    ]
