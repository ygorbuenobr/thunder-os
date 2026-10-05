CREATE TABLE thunder_social_resolution(touch_id INTEGER PRIMARY KEY REFERENCES thunder_social_quarantine(touch_id),family TEXT NOT NULL CHECK(family IN ('curtida','comentario')),post_urn TEXT NOT NULL CHECK(substr(post_urn,1,16)='urn:li:activity:' AND length(substr(post_urn,17)) BETWEEN 1 AND 25 AND substr(post_urn,17) NOT GLOB '*[^0-9]*'),proposal_sha256 TEXT NOT NULL);
CREATE TABLE thunder_social_resolution_evidence(proposal_sha256 TEXT PRIMARY KEY,proposal_json TEXT NOT NULL,mapping_path TEXT NOT NULL,mapping_sha256 TEXT NOT NULL);
CREATE TRIGGER thunder_social_resolution_insert BEFORE INSERT ON thunder_social_resolution BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM thunder_social_quarantine q WHERE q.touch_id=NEW.touch_id AND q.family=NEW.family) OR NOT EXISTS(SELECT 1 FROM thunder_social_resolution_evidence e WHERE e.proposal_sha256=NEW.proposal_sha256) THEN RAISE(ABORT,'social_resolution_unverified') END;
END;
CREATE TRIGGER thunder_social_resolution_update BEFORE UPDATE ON thunder_social_resolution BEGIN SELECT RAISE(ABORT,'social_resolution_immutable'); END;
CREATE TRIGGER thunder_social_resolution_delete BEFORE DELETE ON thunder_social_resolution BEGIN SELECT RAISE(ABORT,'social_resolution_immutable'); END;
CREATE TRIGGER thunder_social_resolution_evidence_update BEFORE UPDATE ON thunder_social_resolution_evidence BEGIN SELECT RAISE(ABORT,'social_resolution_immutable'); END;
CREATE TRIGGER thunder_social_resolution_evidence_delete BEFORE DELETE ON thunder_social_resolution_evidence BEGIN SELECT RAISE(ABORT,'social_resolution_immutable'); END;
CREATE VIEW thunder_social_effective_quarantine AS SELECT q.touch_id,CASE WHEN r.touch_id IS NULL THEN q.scope ELSE 'target' END AS scope,q.family,COALESCE(r.post_urn,q.post_urn) AS post_urn FROM thunder_social_quarantine q LEFT JOIN thunder_social_resolution r ON r.touch_id=q.touch_id;
DROP TRIGGER thunder_social_insert;
CREATE TRIGGER thunder_social_insert BEFORE INSERT ON toques WHEN NEW.canal='linkedin' AND NEW.acao IN ('curtida','comentario') BEGIN
 SELECT CASE WHEN EXISTS(SELECT 1 FROM thunder_social_effective_quarantine WHERE scope='global' OR (family=NEW.acao AND post_urn='urn:li:activity:'||substr(NEW.alvo,54))) THEN RAISE(ABORT,'social_quarantine_unresolved') END;
 SELECT CASE WHEN NEW.status IS NOT 'reservado' THEN RAISE(ABORT,'social_reservation_required') END;
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM thunder_social_writers WHERE writer_id=NEW.agente) THEN RAISE(ABORT,'social_writer_unknown') END;
 SELECT CASE WHEN substr(NEW.alvo,1,53)<>'https://www.linkedin.com/feed/update/urn:li:activity:' OR length(substr(NEW.alvo,54)) NOT BETWEEN 1 AND 25 OR substr(NEW.alvo,54) GLOB '*[^0-9]*' THEN RAISE(ABORT,'social_target_unresolved') END;
END;
