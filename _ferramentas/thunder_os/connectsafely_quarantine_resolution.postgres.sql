-- REVIEW PROPOSAL ONLY. No production application or runtime EXECUTE grants.
-- Default ROLLBACK; requires separately pinned canonical evidence + isolated PG tests.
BEGIN;
SET LOCAL lock_timeout='5s';
LOCK TABLE public.toques IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE thunder_social_coordination.resolution_evidence(proposal_sha256 text PRIMARY KEY,proposal_json jsonb NOT NULL,mapping_path text NOT NULL,mapping_sha256 text NOT NULL);
CREATE TABLE thunder_social_coordination.resolution(touch_id bigint PRIMARY KEY REFERENCES thunder_social_coordination.quarantine(touch_id),family text NOT NULL CHECK(family IN ('curtida','comentario')),post_urn text NOT NULL CHECK(post_urn ~ '^urn:li:activity:[0-9]{1,25}$'),proposal_sha256 text NOT NULL REFERENCES thunder_social_coordination.resolution_evidence(proposal_sha256));
CREATE VIEW thunder_social_coordination.effective_quarantine AS SELECT q.touch_id,CASE WHEN r.touch_id IS NULL THEN q.scope ELSE 'target' END AS scope,q.family,COALESCE(r.post_urn,q.post_urn) AS post_urn FROM thunder_social_coordination.quarantine q LEFT JOIN thunder_social_coordination.resolution r ON r.touch_id=q.touch_id AND r.family=q.family;
CREATE TRIGGER thunder_social_resolution_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.resolution FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
CREATE TRIGGER thunder_social_resolution_evidence_immutable BEFORE UPDATE OR DELETE ON thunder_social_coordination.resolution_evidence FOR EACH ROW EXECUTE FUNCTION thunder_social_coordination.immutable_row();
ALTER TABLE thunder_social_coordination.resolution ENABLE ALWAYS TRIGGER thunder_social_resolution_immutable;
ALTER TABLE thunder_social_coordination.resolution_evidence ENABLE ALWAYS TRIGGER thunder_social_resolution_evidence_immutable;
ALTER TABLE thunder_social_coordination.resolution OWNER TO thunder_social_owner;
ALTER TABLE thunder_social_coordination.resolution_evidence OWNER TO thunder_social_owner;
ALTER VIEW thunder_social_coordination.effective_quarantine OWNER TO thunder_social_owner;
REVOKE ALL ON thunder_social_coordination.resolution,thunder_social_coordination.resolution_evidence,thunder_social_coordination.effective_quarantine FROM PUBLIC;
CREATE OR REPLACE FUNCTION thunder_social_coordination.guard_touch() RETURNS trigger
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
  IF EXISTS(SELECT 1 FROM thunder_social_coordination.effective_quarantine WHERE scope='global' OR (family=NEW.acao AND post_urn=substring(NEW.alvo from 'urn:li:activity:[0-9]+$'))) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
  IF NEW.status IS DISTINCT FROM 'reservado' THEN RAISE EXCEPTION 'social_reservation_required'; END IF;
  IF NOT EXISTS(SELECT 1 FROM thunder_social_coordination.writers WHERE writer_id=NEW.agente AND database_role=session_user) THEN RAISE EXCEPTION 'social_writer_unknown'; END IF;
  IF NEW.alvo IS NULL OR NEW.alvo !~ '^https://www[.]linkedin[.]com/feed/update/urn:li:activity:[0-9]{1,25}$' THEN RAISE EXCEPTION 'social_target_unresolved'; END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION thunder_social_coordination.issue(p_effect text,p_token text,p_account text,p_action text,p_post text,p_request text,p_inventory text,p_ttl integer) RETURNS double precision
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE touch bigint; expiry double precision;
BEGIN
 IF EXISTS(SELECT 1 FROM thunder_social_coordination.effective_quarantine WHERE scope='global' OR (family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END AND post_urn=p_post)) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
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
CREATE OR REPLACE FUNCTION thunder_social_coordination.consume(p_effect text,p_token text,p_account text,p_action text,p_post text,p_request text,p_expiry double precision,p_inventory text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,thunder_social_coordination
AS $$
DECLARE attempt thunder_social_coordination.attempts%ROWTYPE;
BEGIN
 IF EXISTS(SELECT 1 FROM thunder_social_coordination.effective_quarantine WHERE scope='global' OR (family=CASE WHEN p_action='react_post' THEN 'curtida' ELSE 'comentario' END AND post_urn=p_post)) THEN RAISE EXCEPTION 'social_quarantine_unresolved'; END IF;
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
-- No evidence rows are invented or seeded here. Update verifier body pins and
-- readonly catalog ACL in a separately reviewed migration before live readiness.
ROLLBACK;
