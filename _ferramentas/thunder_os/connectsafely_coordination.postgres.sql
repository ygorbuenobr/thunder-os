-- Explicit migration artifact. Apply only to a reviewed isolated/approved DB.
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='thunder_social_owner') THEN CREATE ROLE thunder_social_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
 ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='thunder_social_owner' AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolinherit)) THEN RAISE EXCEPTION 'social_role_unverified'; END IF;
END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='thunder_social_runtime') THEN CREATE ROLE thunder_social_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
 ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='thunder_social_runtime' AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR NOT rolinherit)) THEN RAISE EXCEPTION 'social_role_unverified'; END IF;
END $$;
LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.toques WHERE canal='linkedin' AND acao IN ('curtida','comentario')) THEN RAISE EXCEPTION 'social_backfill_review_required'; END IF;
END $$;

CREATE SCHEMA thunder_social_coordination AUTHORIZATION thunder_social_owner;

REVOKE ALL ON SCHEMA thunder_social_coordination FROM PUBLIC;
CREATE TABLE thunder_social_coordination.writers (
 writer_id text PRIMARY KEY,
 database_role text NOT NULL,
 artifact_sha256 text NOT NULL CHECK(artifact_sha256 ~ '^[a-f0-9]{64}$'),
 audit_sha256 text NOT NULL CHECK(audit_sha256 ~ '^[a-f0-9]{64}$'),
 inventory_hash text NOT NULL CHECK(inventory_hash ~ '^[a-f0-9]{64}$')
);
CREATE TABLE thunder_social_coordination.claims (
 family text NOT NULL CHECK(family IN ('curtida','comentario')),
 post_urn text NOT NULL CHECK(post_urn ~ '^urn:li:activity:[0-9]{1,25}$'),
 touch_id bigint NOT NULL UNIQUE REFERENCES public.toques(id),
 PRIMARY KEY(family,post_urn)
);
CREATE TABLE thunder_social_coordination.backfill(manifest_hash text PRIMARY KEY,manifest_json text NOT NULL);
CREATE TABLE thunder_social_coordination.history(touch_id bigint PRIMARY KEY REFERENCES public.toques(id),row_json text NOT NULL,family text NOT NULL,post_urn text,manifest_hash text NOT NULL REFERENCES thunder_social_coordination.backfill(manifest_hash));
CREATE TABLE thunder_social_coordination.quarantine(touch_id bigint PRIMARY KEY REFERENCES thunder_social_coordination.history(touch_id),scope text NOT NULL CHECK(scope IN ('global','target')),family text NOT NULL,post_urn text,reason text NOT NULL,CHECK((scope='global' AND post_urn IS NULL) OR (scope='target' AND post_urn IS NOT NULL)));
CREATE FUNCTION thunder_social_coordination.guard_touch() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE social_old boolean := false; social_new boolean := false;
BEGIN
 IF TG_OP<>'INSERT' THEN social_old := OLD.canal='linkedin' AND OLD.acao IN ('curtida','comentario'); END IF;
 IF TG_OP<>'DELETE' THEN social_new := NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario'); END IF;
 IF TG_OP='DELETE' AND social_old THEN RAISE EXCEPTION 'social_touch_immutable'; END IF;
 IF TG_OP='UPDATE' AND (social_old OR social_new) THEN
  IF EXISTS(SELECT 1 FROM thunder_social_coordination.history WHERE touch_id=OLD.id) THEN RAISE EXCEPTION 'social_history_immutable'; END IF;
  IF (NEW.id,NEW.canal,NEW.acao,NEW.alvo,NEW.pessoa_chave,NEW.agente) IS DISTINCT FROM
     (OLD.id,OLD.canal,OLD.acao,OLD.alvo,OLD.pessoa_chave,OLD.agente) THEN RAISE EXCEPTION 'social_identity_immutable'; END IF;
  IF NEW.status IS NULL OR NEW.status NOT IN ('reservado','incerto','erro','feito') OR
     (OLD.status='feito' AND NEW.status<>'feito') OR
     (OLD.status IN ('erro','incerto') AND NEW.status='reservado') THEN RAISE EXCEPTION 'social_status_monotonic'; END IF;
  IF OLD.provider_id IS NOT NULL AND NEW.provider_id IS DISTINCT FROM OLD.provider_id THEN RAISE EXCEPTION 'social_receipt_immutable'; END IF;
 END IF;
 IF TG_OP='INSERT' AND social_new THEN
  IF EXISTS(SELECT 1 FROM thunder_social_coordination.quarantine WHERE scope='global' OR (family=NEW.acao AND post_urn=substring(NEW.alvo from 'urn:li:activity:[0-9]+$'))) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
  IF NEW.status IS DISTINCT FROM 'reservado' THEN RAISE EXCEPTION 'social_reservation_required'; END IF;
  IF NOT EXISTS(SELECT 1 FROM thunder_social_coordination.writers WHERE writer_id=NEW.agente AND database_role=session_user) THEN RAISE EXCEPTION 'social_writer_unknown'; END IF;
  IF NEW.alvo IS NULL OR NEW.alvo !~ '^https://www[.]linkedin[.]com/feed/update/urn:li:activity:[0-9]{1,25}$' THEN RAISE EXCEPTION 'social_target_unresolved'; END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION thunder_social_coordination.guard_touch() FROM PUBLIC;
CREATE FUNCTION thunder_social_coordination.claim_touch() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
BEGIN
 IF NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario') THEN
  INSERT INTO thunder_social_coordination.claims(family,post_urn,touch_id)
  VALUES(NEW.acao,substring(NEW.alvo from 'urn:li:activity:[0-9]+$'),NEW.id);
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION thunder_social_coordination.claim_touch() FROM PUBLIC;
CREATE TRIGGER thunder_social_guard BEFORE INSERT OR UPDATE OR DELETE ON public.toques
FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.guard_touch();
CREATE TRIGGER thunder_social_claim AFTER INSERT ON public.toques
FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.claim_touch();
ALTER TABLE public.toques ENABLE ALWAYS TRIGGER thunder_social_guard;
ALTER TABLE public.toques ENABLE ALWAYS TRIGGER thunder_social_claim;
REVOKE ALL ON ALL TABLES IN SCHEMA thunder_social_coordination FROM PUBLIC;

CREATE TABLE thunder_social_coordination.attempts (
 effect_key text NOT NULL, attempt_token text NOT NULL, touch_id bigint NOT NULL REFERENCES public.toques(id),
 account_id text NOT NULL, action text NOT NULL CHECK(action IN ('comment','reply','react_post')),
 post_urn text NOT NULL, request_hash text NOT NULL, state text NOT NULL CHECK(state IN ('issued','consumed','retired')),
 expires_at double precision NOT NULL, PRIMARY KEY(effect_key,attempt_token)
);
CREATE UNIQUE INDEX thunder_social_attempt_active ON thunder_social_coordination.attempts(effect_key) WHERE state IN ('issued','consumed');
CREATE TABLE thunder_social_coordination.audit (
 id bigserial PRIMARY KEY,effect_key text NOT NULL,attempt_token text NOT NULL,event text NOT NULL,at double precision NOT NULL,inventory_hash text NOT NULL
);
CREATE FUNCTION thunder_social_coordination.issue(p_effect text,p_token text,p_account text,p_action text,p_post text,p_request text,p_inventory text,p_ttl integer) RETURNS double precision
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE touch bigint; expiry double precision;
BEGIN
 IF EXISTS(SELECT 1 FROM thunder_social_coordination.quarantine WHERE scope='global' OR (family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END AND post_urn=p_post)) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
 IF p_effect IS NULL OR p_effect !~ '^[a-f0-9]{64}$' OR p_request IS NULL OR p_request !~ '^[a-f0-9]{64}$'
 OR p_token IS NULL OR p_token !~ '^[A-Za-z0-9_.:-]{1,160}$' OR p_account IS NULL OR p_account !~ '^[a-f0-9]{24}$'
 OR p_action IS NULL OR p_action NOT IN ('comment','reply','react_post') OR p_post IS NULL OR p_post !~ '^urn:li:activity:[0-9]{1,25}$'
 OR p_ttl IS NULL OR p_ttl NOT BETWEEN 1 AND 30 THEN RAISE EXCEPTION 'social_capability_scope_invalid'; END IF;
 IF p_inventory IS NULL OR NOT EXISTS(SELECT 1 FROM thunder_social_coordination.writers)
 OR EXISTS(SELECT 1 FROM thunder_social_coordination.writers WHERE inventory_hash<>p_inventory) THEN RAISE EXCEPTION 'social_writer_inventory_changed'; END IF;
 SELECT t.id INTO touch FROM public.thunder_effect_touches e JOIN public.toques t ON t.id=e.touch_id
 JOIN thunder_social_coordination.claims c ON c.touch_id=t.id WHERE e.effect_key=p_effect
 AND c.post_urn=p_post AND c.family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END
 AND t.status='reservado' AND t.provider_id IS NULL
 AND NOT EXISTS(SELECT 1 FROM thunder_social_coordination.history h WHERE h.touch_id=t.id) FOR UPDATE OF t;
 IF touch IS NULL THEN RAISE EXCEPTION 'social_reservation_unverified'; END IF;
 expiry := extract(epoch FROM clock_timestamp())+p_ttl;
 INSERT INTO thunder_social_coordination.attempts VALUES(p_effect,p_token,touch,p_account,p_action,p_post,p_request,'issued',expiry);
 INSERT INTO thunder_social_coordination.audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(p_effect,p_token,'issued',extract(epoch FROM clock_timestamp()),p_inventory);
 RETURN expiry;
END $$;
CREATE FUNCTION thunder_social_coordination.consume(p_effect text,p_token text,p_account text,p_action text,p_post text,p_request text,p_expiry double precision,p_inventory text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE attempt thunder_social_coordination.attempts%ROWTYPE;
BEGIN
 IF EXISTS(SELECT 1 FROM thunder_social_coordination.quarantine WHERE scope='global' OR (family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END AND post_urn=p_post)) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
 IF p_inventory IS NULL OR NOT EXISTS(SELECT 1 FROM thunder_social_coordination.writers)
 OR EXISTS(SELECT 1 FROM thunder_social_coordination.writers WHERE inventory_hash<>p_inventory) THEN RAISE EXCEPTION 'social_writer_inventory_changed'; END IF;
 SELECT * INTO attempt FROM thunder_social_coordination.attempts WHERE effect_key=p_effect AND attempt_token=p_token FOR UPDATE;
 IF NOT FOUND OR attempt.state<>'issued' OR attempt.expires_at<=extract(epoch FROM clock_timestamp())
 OR (attempt.account_id,attempt.action,attempt.post_urn,attempt.request_hash,attempt.expires_at)
 IS DISTINCT FROM (p_account,p_action,p_post,p_request,p_expiry) THEN RAISE EXCEPTION 'social_attempt_fenced'; END IF;
 PERFORM 1 FROM public.toques t JOIN public.thunder_effect_touches e ON e.touch_id=t.id
 JOIN thunder_social_coordination.claims c ON c.touch_id=t.id
 WHERE t.id=attempt.touch_id AND e.effect_key=p_effect AND t.status='reservado' AND t.provider_id IS NULL
 AND c.post_urn=p_post AND c.family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END FOR UPDATE OF t;
 IF NOT FOUND THEN RAISE EXCEPTION 'social_reservation_unverified'; END IF;
 UPDATE thunder_social_coordination.attempts SET state='consumed' WHERE effect_key=p_effect AND attempt_token=p_token;
 INSERT INTO thunder_social_coordination.audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(p_effect,p_token,'consumed',extract(epoch FROM clock_timestamp()),p_inventory);
 RETURN true;
END $$;
CREATE FUNCTION thunder_social_coordination.retire(p_effect text,p_token text,p_inventory text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE phase text;
BEGIN
 IF p_inventory IS NULL OR NOT EXISTS(SELECT 1 FROM thunder_social_coordination.writers)
 OR EXISTS(SELECT 1 FROM thunder_social_coordination.writers WHERE inventory_hash<>p_inventory) THEN RAISE EXCEPTION 'social_writer_inventory_changed'; END IF;
 SELECT state INTO phase FROM thunder_social_coordination.attempts WHERE effect_key=p_effect AND attempt_token=p_token FOR UPDATE;
 IF phase='retired' THEN RETURN true; END IF;
 IF phase IS DISTINCT FROM 'issued' THEN RAISE EXCEPTION 'social_attempt_fenced'; END IF;
 UPDATE thunder_social_coordination.attempts SET state='retired' WHERE effect_key=p_effect AND attempt_token=p_token;
 INSERT INTO thunder_social_coordination.audit(effect_key,attempt_token,event,at,inventory_hash) VALUES(p_effect,p_token,'retired_not_dispatched',extract(epoch FROM clock_timestamp()),p_inventory);
 RETURN true;
END $$;
CREATE FUNCTION thunder_social_coordination.immutable_row() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$ BEGIN RAISE EXCEPTION 'social_history_immutable'; END $$;
CREATE TRIGGER thunder_social_claim_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.claims FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
CREATE TRIGGER thunder_social_audit_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.audit FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
CREATE FUNCTION thunder_social_coordination.guard_attempt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.state<>'issued' OR NEW.state NOT IN ('consumed','retired') OR
 (NEW.effect_key,NEW.attempt_token,NEW.touch_id,NEW.account_id,NEW.action,NEW.post_urn,NEW.request_hash,NEW.expires_at)
 IS DISTINCT FROM (OLD.effect_key,OLD.attempt_token,OLD.touch_id,OLD.account_id,OLD.action,OLD.post_urn,OLD.request_hash,OLD.expires_at)
 THEN RAISE EXCEPTION 'social_attempt_monotonic'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER thunder_social_attempt_monotonic BEFORE UPDATE OR DELETE ON thunder_social_coordination.attempts FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.guard_attempt();
ALTER TABLE thunder_social_coordination.claims ENABLE ALWAYS TRIGGER thunder_social_claim_immutable;
ALTER TABLE thunder_social_coordination.audit ENABLE ALWAYS TRIGGER thunder_social_audit_immutable;
ALTER TABLE thunder_social_coordination.attempts ENABLE ALWAYS TRIGGER thunder_social_attempt_monotonic;
CREATE TRIGGER thunder_social_history_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.history FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
ALTER TABLE thunder_social_coordination.history ENABLE ALWAYS TRIGGER thunder_social_history_immutable;
ALTER TABLE thunder_social_coordination.history OWNER TO thunder_social_owner;
CREATE TRIGGER thunder_social_backfill_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.backfill FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
ALTER TABLE thunder_social_coordination.backfill ENABLE ALWAYS TRIGGER thunder_social_backfill_immutable;
ALTER TABLE thunder_social_coordination.backfill OWNER TO thunder_social_owner;
CREATE TRIGGER thunder_social_quarantine_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.quarantine FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
ALTER TABLE thunder_social_coordination.quarantine ENABLE ALWAYS TRIGGER thunder_social_quarantine_immutable;
ALTER TABLE thunder_social_coordination.quarantine OWNER TO thunder_social_owner;
ALTER TABLE thunder_social_coordination.writers OWNER TO thunder_social_owner;
ALTER TABLE thunder_social_coordination.claims OWNER TO thunder_social_owner;
ALTER TABLE thunder_social_coordination.attempts OWNER TO thunder_social_owner;
ALTER TABLE thunder_social_coordination.audit OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.guard_touch() OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.claim_touch() OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.immutable_row() OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.guard_attempt() OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.issue(text,text,text,text,text,text,text,integer) OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.consume(text,text,text,text,text,text,double precision,text) OWNER TO thunder_social_owner;
ALTER FUNCTION thunder_social_coordination.retire(text,text,text) OWNER TO thunder_social_owner;
GRANT SELECT,UPDATE ON public.toques TO thunder_social_owner;
GRANT SELECT ON public.thunder_effect_touches TO thunder_social_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA thunder_social_coordination FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA thunder_social_coordination FROM PUBLIC;
GRANT USAGE ON SCHEMA thunder_social_coordination TO thunder_social_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA thunder_social_coordination TO thunder_social_runtime;
GRANT EXECUTE ON FUNCTION thunder_social_coordination.issue(text,text,text,text,text,text,text,integer),
 thunder_social_coordination.consume(text,text,text,text,text,text,double precision,text),
 thunder_social_coordination.retire(text,text,text) TO thunder_social_runtime;
-- No writer LOGIN, credential, or grant on public.toques is created here.
-- Separate reviewed deployment must enroll actual writer artifacts and roles.
COMMIT;
