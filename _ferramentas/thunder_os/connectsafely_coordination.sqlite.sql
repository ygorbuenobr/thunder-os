CREATE TABLE thunder_social_backfill(manifest_hash TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
CREATE TABLE thunder_social_history(touch_id INTEGER PRIMARY KEY REFERENCES toques(id), row_json TEXT NOT NULL, family TEXT NOT NULL, post_urn TEXT, manifest_hash TEXT NOT NULL REFERENCES thunder_social_backfill(manifest_hash));
CREATE TABLE thunder_social_quarantine(touch_id INTEGER PRIMARY KEY REFERENCES thunder_social_history(touch_id),scope TEXT NOT NULL CHECK(scope IN ('global','target')),family TEXT NOT NULL,post_urn TEXT,reason TEXT NOT NULL,CHECK((scope='global' AND post_urn IS NULL) OR (scope='target' AND post_urn IS NOT NULL)));
CREATE TABLE thunder_social_writers(writer_id TEXT PRIMARY KEY, artifact_sha256 TEXT NOT NULL, audit_sha256 TEXT NOT NULL, inventory_hash TEXT NOT NULL);
CREATE TABLE thunder_social_claims(family TEXT NOT NULL, post_urn TEXT NOT NULL, touch_id INTEGER NOT NULL UNIQUE REFERENCES toques(id), PRIMARY KEY(family,post_urn));
CREATE TABLE thunder_social_attempts(effect_key TEXT NOT NULL, attempt_token TEXT NOT NULL, touch_id INTEGER NOT NULL, account_id TEXT NOT NULL, action TEXT NOT NULL, post_urn TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('issued','consumed','retired')), expires_at REAL NOT NULL, PRIMARY KEY(effect_key,attempt_token));
CREATE UNIQUE INDEX thunder_social_attempt_active ON thunder_social_attempts(effect_key) WHERE state IN ('issued','consumed');
CREATE TABLE thunder_social_audit(id INTEGER PRIMARY KEY, effect_key TEXT NOT NULL, attempt_token TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL, inventory_hash TEXT NOT NULL);
CREATE TRIGGER thunder_social_insert BEFORE INSERT ON toques WHEN NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario') BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM thunder_social_quarantine WHERE scope='global' OR (family=NEW.acao AND post_urn='urn:li:activity:'||substr(NEW.alvo,54))) THEN RAISE(ABORT,'social_quarantine_unresolved') END;
 SELECT CASE WHEN NEW.status IS NOT 'reservado' THEN RAISE(ABORT,'social_reservation_required') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM thunder_social_writers WHERE writer_id=NEW.agente) THEN RAISE(ABORT,'social_writer_unknown') END;
 SELECT CASE WHEN substr(NEW.alvo,1,53)<>'https://www.linkedin.com/feed/update/urn:li:activity:' OR length(substr(NEW.alvo,54)) NOT BETWEEN 1 AND 25 OR substr(NEW.alvo,54) GLOB '*[^0-9]*' THEN RAISE(ABORT,'social_target_unresolved') END;
END;
CREATE TRIGGER thunder_social_claim AFTER INSERT ON toques WHEN NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario') BEGIN
 INSERT INTO thunder_social_claims(family,post_urn,touch_id) VALUES(NEW.acao,'urn:li:activity:'||substr(NEW.alvo,54),NEW.id);
END;
CREATE TRIGGER thunder_social_update BEFORE UPDATE ON toques WHEN (OLD.canal='linkedin' AND OLD.acao IN ('curtida','comentario')) OR (NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario')) BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM thunder_social_history WHERE touch_id=OLD.id) THEN RAISE(ABORT,'social_history_immutable') END;
 SELECT CASE WHEN NEW.id IS NOT OLD.id OR NEW.canal IS NOT OLD.canal OR NEW.acao IS NOT OLD.acao OR NEW.alvo IS NOT OLD.alvo OR NEW.pessoa_chave IS NOT OLD.pessoa_chave OR NEW.agente IS NOT OLD.agente THEN RAISE(ABORT,'social_identity_immutable') END;
 SELECT CASE WHEN NEW.status IS NULL OR NEW.status NOT IN ('reservado','incerto','erro','feito') OR (OLD.status='feito' AND NEW.status<>'feito') OR (OLD.status IN ('erro','incerto') AND NEW.status='reservado') THEN RAISE(ABORT,'social_status_monotonic') END;
 SELECT CASE WHEN OLD.provider_id IS NOT NULL AND NEW.provider_id IS NOT OLD.provider_id THEN RAISE(ABORT,'social_receipt_immutable') END;
END;
CREATE TRIGGER thunder_social_delete BEFORE DELETE ON toques WHEN OLD.canal='linkedin' AND OLD.acao IN ('curtida','comentario') BEGIN SELECT RAISE(ABORT,'social_touch_immutable'); END;
CREATE TRIGGER thunder_social_claim_update BEFORE UPDATE ON thunder_social_claims BEGIN SELECT RAISE(ABORT,'social_claim_immutable'); END;
CREATE TRIGGER thunder_social_claim_delete BEFORE DELETE ON thunder_social_claims BEGIN SELECT RAISE(ABORT,'social_claim_immutable'); END;
CREATE TRIGGER thunder_social_audit_update BEFORE UPDATE ON thunder_social_audit BEGIN SELECT RAISE(ABORT,'social_audit_immutable'); END;
CREATE TRIGGER thunder_social_audit_delete BEFORE DELETE ON thunder_social_audit BEGIN SELECT RAISE(ABORT,'social_audit_immutable'); END;

CREATE TRIGGER thunder_social_attempt_update BEFORE UPDATE ON thunder_social_attempts BEGIN
 SELECT CASE WHEN OLD.state<>'issued' OR NEW.state NOT IN ('consumed','retired') OR NEW.effect_key<>OLD.effect_key OR NEW.attempt_token<>OLD.attempt_token OR NEW.touch_id<>OLD.touch_id OR NEW.account_id<>OLD.account_id OR NEW.action<>OLD.action OR NEW.post_urn<>OLD.post_urn OR NEW.request_hash<>OLD.request_hash OR NEW.expires_at<>OLD.expires_at THEN RAISE(ABORT,'social_attempt_monotonic') END;
END;
CREATE TRIGGER thunder_social_attempt_delete BEFORE DELETE ON thunder_social_attempts BEGIN SELECT RAISE(ABORT,'social_attempt_immutable'); END;
CREATE TRIGGER thunder_social_backfill_update BEFORE UPDATE ON thunder_social_backfill BEGIN SELECT RAISE(ABORT,'social_history_immutable'); END;
CREATE TRIGGER thunder_social_backfill_delete BEFORE DELETE ON thunder_social_backfill BEGIN SELECT RAISE(ABORT,'social_history_immutable'); END;
CREATE TRIGGER thunder_social_history_update BEFORE UPDATE ON thunder_social_history BEGIN SELECT RAISE(ABORT,'social_history_immutable'); END;
CREATE TRIGGER thunder_social_history_delete BEFORE DELETE ON thunder_social_history BEGIN SELECT RAISE(ABORT,'social_history_immutable'); END;
CREATE TRIGGER thunder_social_quarantine_update BEFORE UPDATE ON thunder_social_quarantine BEGIN SELECT RAISE(ABORT,'social_quarantine_immutable'); END;
CREATE TRIGGER thunder_social_quarantine_delete BEFORE DELETE ON thunder_social_quarantine BEGIN SELECT RAISE(ABORT,'social_quarantine_immutable'); END;
