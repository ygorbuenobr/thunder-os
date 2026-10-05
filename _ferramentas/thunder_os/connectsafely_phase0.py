"""Explicit preservation-only PostgreSQL installation. Never enrolls a writer.

Phase 0 is a separate contract, not an empty/fabricated InventoryEvidence. Every
reviewed social row must remain globally quarantined. No runtime EXECUTE grant,
writer membership, claim, attempt or audit is allowed. Activation needs a later
reviewed migration; normal apply cannot silently promote this installation.
"""
from .connectsafely_coordination import require
from .connectsafely_coordination_postgres import _PG, PG_SQL, verify_schema
from .connectsafely_backfill import dry_run, _review, _seed, verify_backfill, _rollback_check

ROLES = ('thunder_social_owner', 'thunder_social_runtime')
HISTORY_GUARD = "DO $$ BEGIN\n IF EXISTS(SELECT 1 FROM public.toques WHERE canal='linkedin' AND acao IN ('curtida','comentario')) THEN RAISE EXCEPTION 'social_backfill_review_required'; END IF;\nEND $$;"
RUNTIME_EXECUTE = "GRANT EXECUTE ON FUNCTION thunder_social_coordination.issue(text,text,text,text,text,text,text,integer),\n thunder_social_coordination.consume(text,text,text,text,text,text,double precision,text),\n thunder_social_coordination.retire(text,text,text) TO thunder_social_runtime;"


def _begin(connection):
    require(connection.get_transaction_status() == 0, 'social_transaction_already_open')
    con = _PG(connection)
    con.execute('SET LOCAL search_path=pg_catalog,public')
    con.execute("SET LOCAL lock_timeout='500ms'")
    con.execute("SET LOCAL statement_timeout='10s'")
    con.execute('SELECT pg_advisory_xact_lock(871290431)')
    con.execute('LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE')
    return con


def _members(con):
    rows = con.execute("SELECT 1 FROM pg_auth_members m JOIN pg_roles target ON target.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE target.rolname IN ('thunder_social_owner','thunder_social_runtime') OR member.rolname IN ('thunder_social_owner','thunder_social_runtime')").fetchall()
    require(not rows, 'social_phase0_role_membership_forbidden')
    roles = con.execute("SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname IN ('thunder_social_owner','thunder_social_runtime')").fetchall()
    for name, *flags in roles:
        require(not any(flags[:6]) and flags[6] == (name == 'thunder_social_runtime'), 'social_phase0_role_unverified')


def _mapping(con, provision):
    exists = con.execute("SELECT to_regclass('public.thunder_effect_touches')").fetchone()[0]
    if exists is None:
        require(provision is True, 'social_effect_mapping_schema_required')
        con.execute('CREATE TABLE public.thunder_effect_touches(effect_key text PRIMARY KEY,touch_id bigint NOT NULL REFERENCES public.toques(id))')
    kind = con.execute("SELECT relkind FROM pg_class WHERE oid='public.thunder_effect_touches'::regclass").fetchone()
    require(kind == ('r',), 'social_effect_mapping_schema_required')
    # Acquire relation lock before catalog inspection; concurrent DDL cannot race it.
    con.execute('SELECT effect_key,touch_id FROM public.thunder_effect_touches LIMIT 0')
    kind = con.execute("SELECT relkind FROM pg_class WHERE oid='public.thunder_effect_touches'::regclass").fetchone()
    require(kind == ('r',), 'social_effect_mapping_schema_required')
    columns = con.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull FROM pg_attribute a WHERE a.attrelid='public.thunder_effect_touches'::regclass AND a.attnum>0 AND NOT a.attisdropped").fetchall()
    names = {r[0]: (r[1], r[2]) for r in columns}
    require(names.get('effect_key') == ('text', True) and names.get('touch_id') == ('bigint', True), 'social_effect_mapping_schema_required')
    keys = con.execute("SELECT ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num ORDER BY k.ord) FROM pg_index i WHERE i.indrelid='public.thunder_effect_touches'::regclass AND i.indisprimary AND i.indisvalid AND i.indimmediate AND i.indpred IS NULL").fetchall()
    require(keys == [(['effect_key'],)], 'social_effect_mapping_schema_required')
    foreign_keys = con.execute("SELECT c.convalidated,c.condeferrable FROM pg_constraint c WHERE c.conrelid='public.thunder_effect_touches'::regclass AND c.contype='f' AND c.confrelid='public.toques'::regclass AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=c.conrelid AND attname='touch_id')]::smallint[] AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=c.confrelid AND attname='id')]::smallint[]").fetchall()
    require(any(validated and not deferrable for validated,deferrable in foreign_keys), 'social_effect_mapping_schema_required')
    require(not con.execute('SELECT 1 FROM public.thunder_effect_touches e LEFT JOIN public.toques t ON t.id=e.touch_id WHERE t.id IS NULL LIMIT 1').fetchone(), 'social_effect_mapping_orphaned')
    return exists is None


def _plan_required(plan, expected_social_count):
    require(type(expected_social_count) is int and expected_social_count > 0, 'social_phase0_count_required')
    require(plan['social_count'] == plan['quarantined_count'] == plan['global_quarantine_count'] == expected_social_count
            and plan['claim_count'] == plan['target_quarantine_count'] == 0
            and plan['manifest']['mapping_evidence'] is None, 'social_phase0_global_quarantine_required')


def _verify(con, pin, expected_social_count):
    _members(con)
    verify_schema(con)
    counts = verify_backfill(con, pin, allow_quarantine=True)
    require(counts['social_count'] == counts['quarantined_count'] == counts['global_quarantine_count'] == expected_social_count
            and counts['claim_count'] == counts['target_quarantine_count'] == 0, 'social_phase0_global_quarantine_required')
    # Recompute current history as well: no new social activity may be hidden outside
    # the preserved manifest, even if an administrator bypassed INSERT guards.
    plan = dry_run(con)
    _plan_required(plan, expected_social_count)
    _review(plan, pin)
    for name in ('writers', 'claims', 'attempts', 'audit'):
        require(not con.execute('SELECT 1 FROM thunder_social_' + name + ' LIMIT 1').fetchone(), 'social_phase0_activity_forbidden')
    # Only the function owner retains EXECUTE. No PUBLIC/runtime/phase0 caller grant.
    grants = con.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE n.nspname='thunder_social_coordination' AND a.privilege_type='EXECUTE' AND a.grantee<>p.proowner").fetchall()
    require(not grants, 'social_phase0_execute_grant_forbidden')
    return {'installed_locked': True, 'runtime_ready': False, 'manifest_hash': pin, **counts}


def apply_postgres_phase0(connection, *, reviewed_manifest_hash, expected_social_count=67, provision_mapping=False):
    """Admin-only explicit installation; mapping creation is a separate opt-in.

    All DDL, optional prerequisite creation, seeding and verification commit once.
    No policy file, credential, live writer inventory or provider is discovered.
    """
    require(type(provision_mapping) is bool, 'social_phase0_mapping_option_invalid')
    require(connection.get_transaction_status() == 0, 'social_transaction_already_open')
    try:
        con = _begin(connection)
        _members(con)
        plan = dry_run(con)
        _review(plan, reviewed_manifest_hash)
        _plan_required(plan, expected_social_count)
        created = _mapping(con, provision_mapping)
        if con.execute("SELECT to_regclass('thunder_social_coordination.backfill')").fetchone()[0]:
            result = _verify(con, reviewed_manifest_hash, expected_social_count)
            connection.commit()
            return {**result, 'applied': False, 'mapping_created': created}
        require(con.execute("SELECT to_regnamespace('thunder_social_coordination')").fetchone()[0] is None, 'social_phase0_existing_schema_unverified')
        sql = PG_SQL.read_text()
        require(sql.count(HISTORY_GUARD) == sql.count(RUNTIME_EXECUTE) == 1 and sql.endswith('COMMIT;\n'), 'social_migration_invalid')
        sql = sql.replace(HISTORY_GUARD, '').replace(RUNTIME_EXECUTE, '').replace('BEGIN;\n', '', 1)[:-len('COMMIT;\n')]
        connection.cursor().execute(sql)
        _seed(con, plan)
        result = _verify(con, reviewed_manifest_hash, expected_social_count)
        connection.commit()
        return {**result, 'applied': True, 'mapping_created': created}
    except Exception:
        connection.rollback()
        raise


def verify_postgres_phase0(connection, *, reviewed_manifest_hash, expected_social_count=67):
    """Read-only installed-but-blocked proof, usable by the phase0 SELECT role."""
    require(connection.get_transaction_status() == 0, 'social_transaction_already_open')
    try:
        con = _PG(connection)
        con.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        con.execute('SET LOCAL search_path=pg_catalog,public')
        con.execute("SET LOCAL lock_timeout='500ms'")
        con.execute("SET LOCAL statement_timeout='10s'")
        con.execute('SELECT pg_advisory_xact_lock_shared(871290431)')
        con.execute('LOCK TABLE public.toques IN ACCESS SHARE MODE')
        _mapping(con, False)
        result = _verify(con, reviewed_manifest_hash, expected_social_count)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def rollback_postgres_phase0(connection, *, reviewed_manifest_hash, expected_social_count=67):
    """Pre-activity only. Preserve all history, mapping, roles and public ACLs.

    Optional mapping/role removal is deliberately not part of this API. Owner ACLs
    acquired on public tables remain; the operational manifest records this limit.
    """
    require(connection.get_transaction_status() == 0, 'social_transaction_already_open')
    try:
        con = _begin(connection)
        _verify(con, reviewed_manifest_hash, expected_social_count)
        _rollback_check(con, reviewed_manifest_hash)
        con.execute('DROP TRIGGER thunder_social_guard ON public.toques')
        con.execute('DROP TRIGGER thunder_social_claim ON public.toques')
        for name in ('attempts', 'audit', 'claims', 'quarantine', 'history', 'backfill', 'writers'):
            con.execute('DROP TABLE thunder_social_coordination.' + name)
        for signature in ('guard_touch()', 'claim_touch()', 'immutable_row()', 'guard_attempt()',
                          'issue(text,text,text,text,text,text,text,integer)',
                          'consume(text,text,text,text,text,text,double precision,text)', 'retire(text,text,text)'):
            con.execute('DROP FUNCTION thunder_social_coordination.' + signature)
        con.execute('DROP SCHEMA thunder_social_coordination')
        connection.commit()
        return {'rolled_back': True, 'runtime_ready': False, 'mapping_retained': True, 'roles_retained': True}
    except Exception:
        connection.rollback()
        raise
