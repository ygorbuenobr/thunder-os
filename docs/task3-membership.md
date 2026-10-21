# Tarefa 3: membership de um contato

Fatia local delegada, base `2a08b0d4dee895d08b93f49ac22a3bc6ea5ae0ca`,
Mac/Darwin, worktree `/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`,
branch `codex/cadencias-readonly`. Perfil TLC Implement light; revisao independente
pertence ao principal. Este arquivo concentra contrato, checklist e evidencias
da fatia; nao cria outro workflow nem autoriza publicacao.

## Fontes e ownership

- Pedido delegado atual: este documento, `campaign_membership.py`,
  `campaign_membership_hubspot.py` (ownership ampliado pelo principal) e
  `test_thunder_os_campaign_membership.py`; sem commit, deploy ou CRM real.
- Brain `projetos/thunder-os/plano-mestre-thunder-os.md`, integral: Tarefa 3,
  propriedades `campanha_ativa`/`executor`, HubSpot como verdade e nenhum banco novo.
- `.specs/features/thunder-os-v1/spec.md`: validacao no servidor, autorizacao
  existente, efeitos incertos sem retry e sucesso somente com readback.
- `.specs/features/thunder-os-v1/design.md` e
  `campaign-execution-confirmation.md`: Repository, controles existentes e limites
  da serializacao local. Nao importar autoridade de runtimes antigos.
- Brain `cadencia-multicanal/especificacao.md`: estado do lead no HubSpot.
- Reader existente `cadence_readonly.py`: nao substituir nem editar.

## Landing

Uma fatia, quatro arquivos. Reutiliza `CampaignRegistry` e `Repository`; o estado
`campaign_membership_proposal` e somente proposta/resultado interno por contato,
na tabela existente `thunder_state`, nunca cadastro autoritativo de membros.
Nao ha schema, DB, ledger, aprovacao ou fence novo.

| Decisao | Contrato | Motivo |
| --- | --- | --- |
| Executor | plataforma exata do Registry | Compativel com diagnostico do reader; nao inventar nome de agente/provider |
| Writer | porta injetada, `mutation_enabled=False` | Nao ampliar endpoints do HubSpotHTTP nem ligar CRM nesta fatia |
| Concorrencia | CAS da proposta no Repository; releitura do contato | Protege chamadas cooperantes da mesma proposta; nao e CAS remoto/global |
| Resposta | liberar somente campanha esperada, com referencia e nota | Nao limpar campanha que mudou nem declarar parada externa |

## Checks

Provas abaixo usam `unittest` com o modulo
`test_thunder_os_campaign_membership.CampaignMembershipTests`.

- C1: contato ocupado bloqueia a segunda campanha e retorna conflito para UI.
  Prova: `test_lead_ocupado_bloqueia`.
- C2: resposta libera ambas as propriedades e confirma nota no fake.
  Prova: `test_resposta_libera`.
- C3: modo padrao OFF bloqueia writer antes de qualquer efeito, inclusive release.
  Prova: `test_default_off_blocks_assign_and_release`.
- C4: proposta/draft usa Repository e nao altera CRM; snapshot remoto prevalece.
  Prova: `test_draft_uses_repository_but_never_becomes_membership_truth`.
- C5: schema ausente, campo omitido, identidade errada e executor incoerente
  impedem mutacao. Provas: `test_unknown_source_fails_closed`,
  `test_executor_conflict_is_visible`.
- C6: proposta obsoleta, versao concorrente e pausa global bloqueiam writer.
  Provas: `test_stale_contact_or_registry_blocks_write`,
  `test_competing_proposals_use_repository_cas`, `test_global_pause_and_authority`.
- C7: HTTP/porta aceitou sem readback, nota ausente ou timeout nao e sucesso;
  tentativa incerta nao e repetida nem substituida por nova proposta.
  Prova: `test_uncertain_effect_never_retries`.
- C8: mesma associacao e replay confirmado nao repetem efeito.
  Prova: `test_same_membership_and_confirmed_replay_are_noops`.
- C9: release atrasado nao limpa outra campanha, entradas invalidas nao atingem
  provider, e erros nao expoem segredo. Provas:
  `test_release_requires_matching_campaign_and_reply`, `test_input_validation`,
  `test_transport_errors_are_sanitized`.
- C10: adapter HTTP concreto usa GET direto, PATCH restrito as duas propriedades,
  e nota POST com associacao notes->contacts resolvida via metadata, seguida de
  readback. Provas: `test_http_assign_only_patches_membership_properties`,
  `test_http_release_confirms_note_before_clearing_contact`.
- C11: adapter OFF bloqueia antes de carregar credencial/HTTP; metadata ambigua,
  autorizacao ausente, nota incompleta e erro de HTTP nao liberam contato.
  Provas: `test_http_adapter_default_off_before_any_http`,
  `test_http_adapter_failure_boundaries`, `test_http_fixed_transport_and_loader`.
- C12: portal, contato, revisao, schema ou propriedade divergentes impedem todo
  HTTP mutante. Prova: `test_http_target_mismatch_blocks_every_mutation`.

## Swept

Validacao C5/C9; falha parcial, timeout, retry C7; autorizacao/kill switch C3/C6;
concorrencia C6 (somente Repository compartilhado); ciclo de vida C4/C7/C8;
dependencia externa C5/C7/C9; transicoes C1/C2/C9; observabilidade C1/C5/C7.
Sem UI/HTTP, agendamento, background, mudanca de schema ou envio nesta fatia.

## Contrato para integrar

```python
from thunder_os.campaign_membership import CampaignMembership
from thunder_os.campaign_membership_hubspot import CampaignMembershipHubSpot

port = CampaignMembershipHubSpot(mutation_enabled=False)
membership = CampaignMembership(repo, port, writer=port, mutation_enabled=False)
view = membership.inspect(contact_id, campaign_id)
```

O adapter usa `systemd_hubspot_credential`, `HubSpotHTTP` e `_https_request`
existentes, origem fixa `api.hubapi.com:443`, TLS existente, uma tentativa por
request, resposta ate 1 MB e timeout sincrono de 20 s (configuravel entre 0 e 30 s,
exclusive em zero). Nao inicia HTTP no import/construtor. Nenhum segredo e lido
nos testes. Nao monta rota nem consumidor de escrita. O principal integra o GET
somente leitura; o writer pode permanecer desconectado.

Interfaces publicas:

- `inspect(contact_id, campaign_id, *, action='assign', reply_ref=None)`:
  leitura nova por ID exato; nao persiste nem muda CRM.
- `propose(contact_id, campaign_id, *, action='assign', reply_ref=None,
  expected_version=0)`: persiste rascunho/diagnostico no Repository recebido.
- `execute(contact_id, *, expected_version)`: usa proposta armazenada no servidor;
  nao aceita payload arbitrario de browser. OFF na composicao atual.
- `CampaignMembershipError.code`: erro estavel e sanitizado. Portal divergente
  no adapter vira `membership_portal_mismatch`, sem deixar escapar outro tipo.

IDs de contato sao strings decimais positivas com ate 30 digitos; campanhas
usam `[A-Za-z0-9_-]{1,100}` e precisam de Registry ativo/validado. `release`
exige `reply_ref` de 1 a 200 caracteres `[A-Za-z0-9_.:/-]` e campanha atual exata.
Essa referencia deve vir do fluxo autenticado de resposta verificada; sua forma
valida sozinha nao comprova resposta humana. A rota GET de admissao nao libera.

Resultado de `inspect`:

- `status`: `ready`, `unchanged` ou `blocked`; `ready` significa proposta viavel,
  nao inscricao ou permissao de escrita. `reason` e codigo ou `null`.
- `membership`: `{campaign, executor}` da leitura HubSpot, ou `null` se desconhecida.
- `source`: `{provider:'hubspot', portal_id, status, observed_at}`;
  status `verified`, `configuration_pending` ou `unavailable`.
- `contact_conflicts`: lista de `{contact_id, campaign, requested_campaign,
  executor, platform, reason}`. Os valores sao texto/dados, nao HTML seguro.
- `expected`: duas propriedades normalizadas (`null` e `''` significam vazio)
  mais `updated_at`; `desired`: somente as duas propriedades, ou `null` se bloqueado.
- `registry_context`, `action`, `reply_ref`, `note`, `mutation_enabled`,
  `readback_verified`, `external_platforms_stopped` completam a proposta.
- `propose` acrescenta `id`, `version`, `updated_at` do Repository. `execute`
  retorna `confirmed`, `uncertain` ou `attempted` persistido e `note_id` quando
  confirmado. Falha antes da tentativa gera erro; falha depois fica incerta.

Conflitos principais: `lead_occupied`, `release_campaign_mismatch`,
`executor_without_campaign`, `executor_missing`,
`executor_differs_from_registry_platform`. Campo omitido/schema ausente nunca e
lead livre. Proposta local nao participa da atribuicao do reader nem substitui a
leitura HubSpot. Replay confirmado e evidencia da tentativa anterior; `inspect`
e a consulta do estado atual.

## Interruptor e adapter concreto

`mutation_enabled=False` no core e no adapter e independente de
`control.global.external_effects_enabled`, inclusive quando esse flag legado e
`true`. Sem o interruptor do core, `execute` para antes de qualquer HTTP. Sem o
interruptor do adapter, `write_membership` para antes de credencial/HTTP, mesmo
se o core for ligado por engano. Nenhum env habilita essa capacidade.

Uma futura ativacao exige autorizacao explicita e composicao revisada que injete
`authorize(command)` do mecanismo existente para o comando exato, credencial,
escopo/teto e pausa/kill switch; nao basta `env=true`, ator textual ou callback
que retorna sempre `True` (usado apenas nos fakes). Nao ha approval kind novo.
O core respeita `control.global.paused` e reler/autorizar ocorre antes do efeito;
o adapter reavalia seu authorizer antes de cada HTTP mutante. A rota GET atual
deve manter os dois switches OFF e nao montar POST/execute.

Assign faz somente `PATCH /crm/v3/objects/contacts/{id}` com `properties` contendo
exatamente `campanha_ativa` e `executor`. Release primeiro resolve
`GET /crm/v4/associations/notes/contacts/labels`, exigindo um unico tipo sem label
`HUBSPOT_DEFINED` na direcao notes->contacts; nao fixa numero de associacao.
Cria `POST /crm/v3/objects/notes` com `hs_timestamp`, `hs_note_body` e associacao
ao contato. Confere corpo e associacao por GET da nota antes de limpar os campos;
relendo contato/schema/portal antes do PATCH. O core reler contato e nota depois
e so entao marca `confirmed`. Nao solicita permissao de EMAIL nem escreve emails,
sequencias, propriedades, tarefas ou inscricoes em plataformas externas.

Referencias oficiais consultadas para o wire contract:
[Notes v3](https://developers.hubspot.com/docs/api-reference/legacy/crm/activities/notes/guide),
[Contacts SDK oficial, PATCH e limpeza](https://github.hubspot.com/hubspot-api-nodejs/classes/crm_contacts.BasicApi.html),
[Associations](https://developers.hubspot.com/docs/api-reference/crm-associations-v4/guide).

## Limites

- Check/releitura + PATCH nao oferece CAS remoto. Outra plataforma ou outro
  escritor pode mudar o contato depois do preflight. Nao ha exclusao global,
  comprovacao de inscricao externa ou deteccao de atribuicoes ocultas fora do CRM.
- CAS da proposta serializa participantes no mesmo Repository, nao todos os
  sistemas. Tentativa e gravada antes do efeito; crash conserva `attempted`.
  `attempted`/`uncertain` nao aceitam nova proposta nem repeticao automatica.
  Reconciliacao operacional posterior pertence ao principal; nao ha reset/retry.
- Nota e PATCH nao sao atomicos: nota pode existir sem liberar o contato, ou PATCH
  ter ocorrido com resposta perdida. Mantem `uncertain`, sem compensacao/retry.
- `external_platforms_stopped=false` sempre. Liberar propriedades nao pausa Apollo,
  Instantly, Chattie ou outros executores, nem remove supressao. Tarefa 4 e separada.
- Chamadas sincronicamente aguardadas, sem tarefas abandonadas em background;
  cada request tem deadline de socket, nao um limite total de toda a operacao.
  Integracao em servidor async deve usar o executor de trabalho existente.
- Integracao HTTP foi exercitada com doubles; permissoes/token, API real, prazos
  reais, propagacao externa e jornada em producao nao foram validados aqui.

## Evidencia e handoff

Validacao local final em Python 3.14.3:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest \
  test_thunder_os_campaign_membership test_thunder_os_campaign_registry \
  test_thunder_os_cadence_contact_campaign
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_source_boundary.py
```

Resultado: **57 testes PASS** (21 membership, 13 Registry, 23 reader) e
`source boundary ok: 187 tracked and unignored source paths`. O ajuste final de
erro de portal foi validado sem enfraquecer o teste de zero mutacoes.
Fixtures SQLite apenas em `.scratch/task3-membership/`, removidas pelo
`TemporaryDirectory` ao fim de cada teste; a pasta vazia permanece. Rede proibida
por mocks durante os testes; o teste de origem fixa simula a propria conexao TLS.

C1-C12 satisfeitos no nivel local/doubles; UI/HTTP do principal e integracao real
nao sao evidencia desta fatia. Schema real criado e reader publicado foram
informados pelo principal, sem nova leitura externa por este especialista.
Independent reviewer pendente, sob coordenacao do principal. Nenhum commit,
push, deploy, CRM real, envio ou pausa externa. Arquivos alheios preservados.
