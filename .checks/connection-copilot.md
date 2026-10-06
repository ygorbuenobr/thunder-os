# Thunder OS Connection Copilot

## Checkpoint 2026-10-06: backfill e identidade

- Falha de 00:00 -03 no backfill de produção: `connection_row_invalid`. A resposta atual do ConnectSafely não a reproduziu; um replay completo com a release ativa em clone processou 100 perfis. Causa exata da resposta transitória permanece desconhecida.
- Clone consistente: `/opt/thunder-os/staging/connection-copilot/clone-backfill-20261006.db`, cópia por SQLite backup, modo 0600, integridade `ok`. No clone, uma linha artificial com `connectedDate=null` reproduziu `connection_row_invalid` e deixou o checkpoint inalterado. O código candidato relê só essa página uma vez, sem retry de timeout ou 429; falha persistente continua bloqueando.
- Ensaio do código candidato em staging VPS, com primeira página artificial inválida e páginas seguintes reais: 100 perfis processados no clone, checkpoint de 2026-10-06 índice 203 para 2026-10-07 índice 303, integridade `ok`, nenhum job criado. O checkpoint de produção permaneceu 2026-10-05, 100 perfis, índice 100, integridade `ok`.
- O GET `/linkedin/profile` da conta ativa retornou 200 e mostrou `currentCompany` e `experience[].companyName` como strings; não forneceu domínio/ID empresarial verificável na amostra. O código novo registra apenas candidata com fonte e exibe "confirmar empresa" no painel. Nome homônimo ou parecido **não** vira `account_id`, request, job ou aprovação. Confirmação canônica continua via perfil exato em ficha autorizada.
- Consulta de perfis limitada a 2 por rodada e 100 reservas por dia/conta. Falha por perfil fica registrada para não repetir no mesmo dia; 429 bloqueia novas consultas da conta até o dia seguinte. Há no máximo uma releitura de linha inválida por processo, preservando a margem do timeout dos timers. A ativação do worker continua bloqueada, pois não há dossiê qualificado e jornada de ponta a ponta observados.
- Validação local: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest discover -s _ferramentas -p 'test_*.py' -q` passou 238 testes. `python3 scripts/check_source_boundary.py` aceitou 132 arquivos rastreados no índice; `git diff --cached --check` limpo. Revisão independente inicial rejeitou promoção por nome e starvation; a segunda identificou retry após 429 e janela de crash. A reserva `in_flight` pré-GET e commit conjunto do resultado/bloqueio corrigiram o segundo ponto. A re-revisão final aceitou o gate de código para commit/build, mas não verificou ativação de monitor nem liberou o worker.
- No clone, o GET real de dois perfis retornou sem erro, `candidates=0`, checkpoint inalterado, nenhum job/request, integridade `ok`. Uma observação sintética posterior no mesmo clone produziu duas candidatas sem `account_id` e sem novos jobs/requests. Isso demonstra o contrato, não a identidade empresarial dessas pessoas reais.

### Ativação controlada cf5f4e3

- Commit de código `cf5f4e3b599fcef446e8c77fb4b286568906c902` enviado a `ygorbuenobr/thunder-os/main`. Bundle local e release VPS de 93 arquivos passaram verificação de hashes e dependência. Painel isolado contra o clone respondeu HTTP 200.
- Antes da troca: snapshot SQLite consistente em `/opt/thunder-os/staging/connection-copilot/recovery-pre-cf5f4e3/state.db` (modo 0600, SHA-256 `10ad467e034417d287fa95d0c922b0d6bdf305d7131353333856fc8477e6ede2`, `integrity_check=ok`). Config e unidades também foram preservadas no diretório 0700. Forward/rollback de symlink e restore de banco foram ensaiados em diretório temporário; não houve rollback real de serviço nem restauração de produção.
- Timers foram parados durante a janela. `/opt/thunder-os/current` passou de `d562788c59853980d9a6091df7069393611f69de` para `cf5f4e3b599fcef446e8c77fb4b286568906c902`; `/opt/thunder-os/agent-current` permaneceu em `d562788`. Apenas `painel-thunder.service` foi reiniciado: active, PID 232118, cwd na nova release, HTTP local 200. Nenhum serviço Hermes/GTM/Cockpit/HunterV2 foi reiniciado.
- Execução manual da unidade live: `Result=success`, `observed=1`, `company_resolution.checked=2`, `candidates=0`, `jobs_ensured=0`; banco íntegro. Execução manual da unidade backfill: `Result=success`, `observed=100`, `company_resolution.checked=2`, `candidates=0`, `jobs_ensured=0`; checkpoint real agora dia 2026-10-06, `processed_today=100`, índice 204, `complete=false`. Perfil contabilizado: 196 `needs_identity`, 8 `needs_fit`, 0 requests de pesquisa. `agent_control.paused=true`.
- Timers live e backfill voltaram ativos. O timer live disparou sozinho às 09:40 -03: `Result=success`, `observed=0`, `company_resolution.checked=2`, `jobs_ensured=0`. Readback final: painel HTTP 200, quatro serviços/timers consultados ativos, produção íntegra, checkpoint de backfill ainda em índice 204, 196 `needs_identity`, 8 `needs_fit`, 0 research requests e `agent_control.paused=true`. Worker **não** foi despausado. Como o GET de perfil não dá domínio/ID empresarial na amostra, candidata por nome não é promoção canônica. O painel continua exigindo confirmação da empresa para esses casos.

Rollback do código (sem restaurar banco automaticamente): parar os dois timers; sob `flock -n /run/lock/thunder-os-deploy.lock`, trocar atomicamente `/opt/thunder-os/current` de volta para a release `d562788c59853980d9a6091df7069393611f69de`; reiniciar somente `painel-thunder.service`; conferir PID/cwd, HTTP 200, banco íntegro e `agent_control.paused=true`; religar timers somente após validar a jornada anterior. O banco novo usa estados adicionais que o release antigo ignora. Restaurar o snapshot é uma decisão separada: apagaria qualquer escrita legítima no painel depois da captura e exige reconciliação antes.

Comandos manuais de rollback do código (rodar apenas se necessário):

```bash
ssh minha-vps 'systemctl stop thunder-os-connections-live.timer thunder-os-connections-backfill.timer'
ssh minha-vps 'flock -n /run/lock/thunder-os-deploy.lock python3 -' <<'PY'
import os
from pathlib import Path
current = Path('/opt/thunder-os/current')
assert current.resolve().name == 'cf5f4e3b599fcef446e8c77fb4b286568906c902'
next_link = current.with_name('current.rollback.next')
assert not next_link.exists() and not next_link.is_symlink()
next_link.symlink_to('/opt/thunder-os/releases/d562788c59853980d9a6091df7069393611f69de')
os.replace(next_link, current)
PY
ssh minha-vps 'systemctl restart painel-thunder.service && systemctl is-active painel-thunder.service && curl -fsS -o /dev/null http://127.0.0.1:8790/'
```

Após isso, validar checkpoint/integridade e iniciar apenas os timers cujas leituras com o release anterior forem saudáveis. O backfill anterior falhou à meia-noite; não o reativar cegamente após um rollback.

Sources:

- User-approved Thunder OS connection, research and queue plan in this task - binding behavior and rollout order.
- `AGENTS.md` and `.specs/features/thunder-os-v1/` - existing product boundaries; the explicit source-ownership decision in this task supersedes the source-only deployment restriction.
- Active VPS units and releases observed on 2026-10-05 - runtime baseline, not an implementation authority.

## Out of scope

- Automatic LinkedIn sends, invitations, reactions or comments - this flow only reads and prepares work.
- Thunder GTM, Cockpit, HunterV2 and Chattie implementation or data migration - separate owners.
- Paid enrichment or model calls without a finite, configured budget and an observed cost receipt.

## Landing

The `thunder-os` repository becomes the source/build owner of the OS panel, agent and monitor, while operational state remains in the VPS data location. ConnectSafely is the observation source for social state, not for company identity. New-connection polling and historical backfill have separate checkpoints.

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| Source authority | Versioned Thunder OS assembly from `ygorbuenobr/thunder-os` | Continuing to build OS releases from `/srv/thunder-gtm.git`, which conflates product authorities |
| Connection identity | Canonical LinkedIn profile key scoped to ConnectSafely account, with provenance for account mapping | Name/headline match, which can link the wrong company |
| External state | Positive readback closes a pending card; unavailable readback remains inconclusive | Treating an old dashboard label or local send attempt as provider proof |

## Checks

### S1 - Source and recovery authority

**C1** - Candidate assembly contains the active OS panel, agent and monitor entrypoints with a single Thunder OS source SHA.
Proof: `python3 scripts/check_runtime_bundle.py <candidate>` and effective unit inspection before activation.

**C2** - Snapshot integrity and an isolated rollback rehearsal pass for panel and agent before either service changes.
Proof: candidate recovery preflight and temporary-symlink rollback test with checksum output.

### S2 - Connection ledger and polling

**C3** - A complete ConnectSafely poll records account-scoped canonical profiles and advances only its live checkpoint.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_complete_live_poll`.

**C4** - Incomplete, reordered or wrong-account pages leave both checkpoint and connection ledger unchanged.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_invalid_poll_is_atomic`.

**C5** - Repeating a poll does not duplicate a connection or research request.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_repeated_poll_is_idempotent`.

### S3 - Historical triage and research

**C6** - Backfill processes at most 100 historical profiles per day without moving the live cursor.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_backfill.ConnectionBackfillTests.test_daily_limit_and_cursor_isolation`.

**C7** - Unknown or conflicting company identity stays in identification work; only evidenced fit enters research. The queue admits at most 5 new research jobs per Sao Paulo day, and waiting profiles resume on later days.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_backfill _ferramentas.test_thunder_os_connection_runtime` (6 tests passed before the extra invalid-page test; rerun consolidated after final edits).

**C8** - Research result, sources, gaps and next action remain correlated to the original profile/account and survive a worker restart without duplicate execution.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_worker.ConnectionWorkerTests.test_restart_keeps_one_job`.

### S4 - Panel and existing-card reconciliation

**C9** - Panel differentiates identity, research, dossier review, approach approval and no-action states. Approach approval requires a current, profile-targeted draft cadence tied to the research hash and sources; account-level copy alone is insufficient. This is display only, not send authorization.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_panel -q` (10 local tests passed).

**C10** - Positive ConnectSafely readback may propose closure of already-completed cards with evidence; missing readback never marks a send complete. Runtime pagination and an atomic, compare-before-write applicator are implemented locally. Before `--apply`, the CLI creates a recoverable receipt journal. A post-commit receipt failure retains that journal, and `--recover-receipt` reconstructs the item dispositions from the panel DB without another provider call. The applicator has not touched production.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_reconcile _ferramentas.test_thunder_os_connection_reconcile_runtime _ferramentas.test_thunder_os_connection_reconcile_apply _ferramentas.test_thunder_os_connection_reconcile_cli` (mocked provider, temporary SQLite and post-commit receipt-failure injection); real provider audit and 41-card disposition remain open.

**C11** - The existing 41-card baseline (21 DMs, 18 comments, 2 WhatsApps observed on 2026-10-05) is classified item by item by channel and provenance, without deleting history or resending Diego's message. Cards without a channel-specific positive receipt remain inconclusive.
Proof: sanitized before/after reconciliation report with one disposition per original card and channel-specific read-only provider receipts where available.

### S5 - Production journey

**C12** - One new and one historical connection traverse observed provider read, persisted state, qualified dossier and panel display in the active VPS runtime, with zero outbound sends.
Proof: correlated runtime receipts, provider GET evidence, database readback and panel browser check after serial activation.

## Swept

- Validation: C4, C7.
- Failure modes: C4, C8, C10.
- Idempotency and retry: C5, C8.
- Authorization: outbound excluded; C9 only exposes review.
- Concurrency and ordering: C4, C5, C8.
- Data lifecycle: C11 preserves existing history.
- External dependency failure: C4, C10.
- State transitions: C3, C7, C9, C10.
- Observability: C11, C12.

## Handoff

Single coordinated build across source, runtime and panel. Do not hand off mid-slice; independent reviewer receives the final diff and all proofs after the last slice.

## Evidence so far (2026-10-05)

### Current operational checkpoint

- Commit `d562788c59853980d9a6091df7069393611f69de` built a 92-file hashed bundle. The VPS staged and installed copies passed the same manifest/contract check. The web and agent symlinks both resolve to this release; web PID 3475529 had that release as cwd and returned local HTTP 200, and agent PID 3478505 had that release as cwd after restart. Both services and the live/backfill timers were active at the 21:23 -03 check.
- Before service changes, `/opt/thunder-os/staging/connection-copilot/recovery-pre-d562788/painel.db` was created with SQLite backup (`integrity_check=ok`, SHA-256 `68c2780f40d7498d51cbd4f45a71395d7f9f9bd44982e96bd2fa851893ce32e6`, mode 0600). Unit copies and a temporary-symlink forward/rollback rehearsal are in the same recovery directory. Previous release `993e0fc` remains available; no DB restore was needed.
- The external lock `/etc/thunder-os/hermes-ro-runtime-lock-v2.json` is root-owned mode 0600 in a root-owned 0755 directory, SHA-256 `0d1ab750f18c34bae8723f106b30834c8c958cea2756aedab3ef0ebc0c4f42bb`. `systemd-analyze verify thunder-os-agent.service` passed; effective `ExecStart` uses the clean Hermes root, and effective `ReadOnlyPaths` include that root and `/etc/thunder-os` with `ProtectSystem=strict`. A transient process with equivalent filesystem restrictions resolved `hermes-auth-store` and `gpt-5.6-terra` without a model call; attempts to write in both read-only paths failed.
- The first web readiness probe ran before its socket opened and got connection refused. A subsequent independent read found active PID/cwd and HTTP 200. This was a startup race in the probe, not evidence of a continuing web outage.
- Current canonical production DB (`integrity_check=ok`): 93 `needs_identity`, 7 `needs_fit`, zero `connection_research_request` rows and zero research work items. Agent health remains `paused`/`agent_paused`. The daily historical timer next runs at 00:00 -03; live polling remains scheduled every ten minutes. No model request, outbound LinkedIn action, or qualified-dossier canary was performed in this activation.
- Independent read-only VPS review confirmed release/cwd/unit alignment, `agent_control` paused and the same 93/7/0 state. It did not prove absence of sends by other channels: the panel unit retains external capabilities, and global provider effects were not audited in this review.
- Two bounded GET-only reads of one `needs_identity` LinkedIn profile via the official ConnectSafely profile endpoint returned HTTP 200. The response exposed a `currentCompany` string and `experience` entries, but no verified company domain/ID in the observed profile shape. No personal field value was printed or persisted. A transport-only experiment was removed before commit: it cannot safely turn company text into canonical account identity. The remaining contract needs a sourced company-to-domain resolution and ambiguity gate before any dossier job. This is a functional implementation gap, not an API outage.
- The VPS web and agent symlinks now resolve to the verified `993e0fca3f902c567895e8a35e47d8b0b20a3cb6` release. The web service returned HTTP 200 locally. The agent is active but its canonical control state is `paused=true`; an active process is not evidence of a completed research turn.
- The read-only ConnectSafely live and historical timers are enabled. The first production historical batch advanced exactly 100 profiles: 93 `needs_identity`, 7 `needs_fit`, 0 research jobs. The 10 baseline history rows were reclassified by the `993e0fc` correction; no dossier can be claimed from this batch.
- The exact original 41-card manifest has SHA-256 `2802b624bc7614f972d73dced86340e9c9df06659105f76d1b9b9e37b777859e`. A fresh full GET-only audit and canonical-lock apply produced 8 superseded cards and 33 inconclusive cards, preserving history. Diego's superseded card is not proof the suggested text was sent. Receipt: `/opt/thunder-os/staging/connection-copilot/legacy-41-apply-993e0fc.json`, SHA-256 `982a303700a2be88ad6067801c62fbc7608654b26f4dc1dd63dc09156fe052f5`.
- Isolated Hermes auth preflight initially failed because the pinned read-only dependency root had 92 unlisted Python cache files. A new clean root `/opt/hermes-agent-ro-thunder-os-09544f73e610` contains the same 14,745 manifest files with matching hashes and no cache files. An external staged lock changing only its root paths passed dependency verification and read-only authentication preflight (`hermes-auth-store`, `gpt-5.6-terra`); no model turn was executed. The unit/lock change is under independent review and not yet active.
- C12 remains open: no qualified dossier and no observed new-plus-historical end-to-end panel journey. The 33 inconclusive old cards remain pending without positive provider evidence. Do not unpause the agent or claim complete activation from tests, timers, or HTTP health alone.

- VPS effective web release `7bc74b02a7aef6f00f2fab097163b7d662462b35` and agent release `a235b97241c9b2e5a08b66a78c709f94d300a2dd` remain active; neither was modified. New source is local only.
- Read-only ConnectSafely GET `/linkedin/accounts` and `/linkedin/connections` returned HTTP 200 with expected pagination keys for the configured account. No profile or message content was printed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest discover -s _ferramentas -p 'test_*.py' -q` passed 211 tests after the latest local changes. These are mock/temporary-DB checks, not a VPS journey.
- Canonical panel DB read-only baseline: 41 `pendente` cards (21 DM, 18 comments, 2 WhatsApps); 12 lack `texto`. A bounded comparison against the already persisted inbox observations found no unique exact positive DM match. This is absence of proof, not proof that no message was sent.
- Active agent unit's `PYTHONPATH` does not expose bundled `_ferramentas/toques.py` to an absolute import. Candidate/agent activation requires an explicit unit adjustment and isolated import proof.
- The active ConnectSafely account returned HTTP 200 for a GET-only connection page; its `connectedDate` format was observed as `Conexão feita em 5 de outubro de 2026`. A targeted contract test for that format passed.
- A candidate from commit `0cc1f67` was built and verified in local and VPS staging (91 hashed files). Its isolated panel answered HTTP 200 against a SQLite clone; panel and agent production symlinks remained unchanged. The first real 41-card manifest preflight blocked because the live monitor config is schema v2, whereas that candidate accepted only v1. It was preserved as `superseded-0cc1f67`; the accepted v2 fix was included in the next candidate.
- Candidate `0f666a6` passed local and VPS bundle integrity checks. A manifest of the exact 41 current pending cards was generated from the live DB with SHA-256 `2802b624bc7614f972d73dced86340e9c9df06659105f76d1b9b9e37b777859e`. The full read-only provider audit timed out at 240 seconds without a receipt and did not mutate a card. Audit-only chunks and a cross-chunk consolidation, including duplicate-receipt rejection, are under local review; 215 local tests pass. They are not in this candidate yet. No production symlink or unit changed.
- Candidate `f601e21` was verified on the VPS. Fourteen GET-only audit chunks covered the original 41-card manifest, and consolidation completed with 8 `superseded`, 33 `inconclusive`, 0 `close`, 0 applied. Diego's card `ev-1cc53c85f59c` is `superseded` by a later thread event, not a verified send of the suggested text. The consolidated receipt is `/opt/thunder-os/staging/connection-copilot/legacy-41-consolidated-f601e21.json` (mode 0600). This does not authorize write from provisional chunks; any apply requires fresh full readback and canonical panel locking.
- A live-provider backfill canary against the SQLite clone stopped before provider access on an invalid LinkedIn URL in a brain ficha. A bounded parser correction now rejects the entire malformed URL while preserving valid exact mappings and ambiguity failures. The apply CLI also checks the canonical brain lock path, but its match to the effective panel process root remains a live preflight gate. Local full suite: 218 tests; these changes are not in candidate `f601e21` yet.
- Candidate `17b218c` passed bundle checks on VPS. The clone backfill advanced past catalog parsing, then rejected a real full-English-month `connectedDate`. A GET-only shape check of 100 rows found 10 Portuguese, 15 full October, and 75 full September dates; all identity fields had the expected string types. The date parser now covers these observed forms and rejects unknown months; 219 local tests pass and focused independent review accepted the change. No live backfill checkpoint or production queue was changed.
- Candidate `7f3aea2` reached the first 100 real provider rows against the isolated SQLite clone, then rejected 9 LinkedIn public identifiers containing Unicode lowercase letters. A sanitized GET-only shape check confirmed each URL slug equals its vanity name and retains the `/in/` path. The parser now accepts Unicode alphanumeric slug characters with the same host/path and exact vanity checks, while rejecting separators and escapes. This is still a clone-only correction; production state and services remain unchanged.
- Candidate `abb8aef` processed 100 real historical rows on the isolated clone: 93 `needs_identity`, 7 `needs_fit`, 0 research jobs; a separate live-poll clone canary observed 0 newer connections. The cloned DB passed integrity and its checkpoint recorded exactly 100 processed profiles. A consistent production DB backup was restored and verified with the same SHA-256; candidate web answered HTTP 200 on isolated port 18790. An initial transfer was rejected for unlisted `__pycache__` files and preserved outside releases; the clean 91-file transfer passed bundle verification. No service was switched. Independent review then found Unicode `casefold()` collisions (`straße`/`strasse`) and a non-idempotent `İ` key, so this candidate is blocked for activation. The parser now preserves non-ASCII identity characters while lowercasing ASCII only, without NFC normalization that would conflate `K` and `K`; focused collision and idempotency tests pass. Retest and re-review are required before activation.
- Release `3641dc1` passed bundle verification and isolated old/new panel HTTP checks, then replaced only the web symlink under a deploy flock. Effective panel PID and cwd matched the new release; HTTP returned 200. The new live-intake service ran once successfully, followed by a 100-profile historical backfill on production with no outbound calls other than ConnectSafely GET. Readback found 83 `needs_identity`, 7 `needs_fit`, and 10 baseline `historical` profiles: the baseline rows were skipped when backfill encountered existing profile records. Timers were not enabled. This is a real triage gap; reclassification of baseline history must pass clone and production readback before scheduling.
- New panel source defaults to skipping legacy `materializar_aceites()` during manual control reconciliation. The old acceptances timer is still active and untouched; its output remains historical input, not authority for a new approval card.
- A matching DM/comment must now have a parseable card creation time and a later provider event. A later *different* DM can only supersede the draft, never prove that its suggested text was sent.
- The first independent review rejected activation for shared-account panel regression, count-only/one-shot reconciliation, and an unreachable approach-review stage; the next review accepted those local fixes but found a post-commit receipt gap in the CLI. A pre-apply journal, directory sync before commit, recovery command and injected failures at replacement and directory sync were added. The reviewer re-ran 29 focused tests and accepted these local fixes, explicitly **not** approving production apply. Candidate build, snapshot/rollback rehearsal, serial deployment, real 41-card audit and end-to-end canaries remain pending.
