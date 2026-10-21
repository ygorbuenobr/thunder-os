# Tarefa 4: fatia interna de parada por resposta

Base: `1c34da08c58abc2acf69aa9272ad884e622e5854`, Mac/Darwin,
usuario `ygorbueno`, branch `codex/cadencias-readonly`, checkout
`/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`.

## Fontes e ownership

Lidos: `SISTEMA.md` e `projetos/thunder-os/plano-mestre-thunder-os.md`
integrais no Brain; spec/design e contrato de confirmacao da V1; Repository,
ReplyCoordinator, CadenceEngine, CampaignRegistry e gates do WorkerRuntime.
Esta e a fatia permitida pelo pedido delegado atual, que restringe a Tarefa 4
do masterplan. Nao satisfaz seu aceite em producao.

Ownership final: somente este documento e
`_ferramentas/thunder_os/reply_control.py` pertencem ao especialista.
`_ferramentas/test_thunder_os_reply_control.py`, inicialmente criado pelo
especialista, foi transferido ao principal por instrucao explicita. Nenhuma
edicao posterior a transferencia. O principal possui HTML, servidor, testes,
boundary e service.py. Workflow SDD existente, sem novo workflow ou gate.
O principal providencia o revisor independente. Sem commit/push.

## Landing

ReplyCoordinator recebe uma ponte de store para o estado `reply` existente e
uma ponte que adapta `pause_for_reply(person_key, reply_id)` para
`CadenceEngine.pause_for_reply(steps)`. Pausas e `operator_alert` usam o mesmo
Repository/SQLite e sua transacao existente. Nenhuma tabela, fence, ledger ou
approval type novo. Leitura automatica nao conectada.
`ingest` rejeita transacao externa ja aberta antes de alterar estado: a pausa,
resposta e alerta precisam confirmar ou reverter juntos nesta chamada.

| Decisao | Contrato | Alternativa rejeitada |
| --- | --- | --- |
| Chave | hash de provider + account_id + provider_message_id | timestamp/conteudo nao identifica entrega |
| Replay | ignora received_at; demais campos vinculados | aceitar alteracao silenciosa da mensagem |
| Identidade | ID exato ou mapeamento canonical/ygor em identity | nome/email isolado nao comprova contato |
| Providers | preparadores locais, sem transporte; OFF obrigatorio | interruptor ativavel nesta fatia proibida |

## Provas atuais do principal

O principal assumiu e substituiu o arquivo de testes. Os nomes atuais em
`test_thunder_os_reply_control.ReplyControlTests`, conferidos no handoff, sao:

- `test_resposta_linkedin_pausa_apollo`: pausa interna e Apollo OFF.
- `test_resposta_instantly_pausa_linkedin`: passos sent/uncertain preservados e
  gate final existente nega a cadencia pausada.
- `test_marcar_respondeu_manual`: replay com received_at diferente e conflito
  por alteracao de conteudo.
- `test_falha_pausa_externa_alerta`: diagnostico fake vira alerta urgente.
- `test_unknown_auto_identity_and_account_do_not_pause`: humanidade/identidade
  desconhecida e conta divergente ficam em triagem.
- `test_atomic_failure_rolls_back_pause_reply_and_alert`: rollback conjunto.
- `test_concurrent_replay_has_one_reply_and_alert`: duplicidade concorrente.
- `test_scope_does_not_pause_another_contact_or_change_crm`: isolamento por ID.
- `test_activation_and_untrusted_actor_rejected`: OFF e fronteira de ator.
- `test_async_acceptance_is_not_provider_pause`: 202 e capacidades UNVERIFIED.
- `test_snapshot_does_not_write`: snapshot sem gravacao de estado.

O principal ampliou para 13 testes do modulo e 14 de painel: incluem rejeicao
de transacao externa, cadencia terminal e legada sem person_key, e composicao
real da rota manual com persistencia e replay. Mapping canonical e execucao
completa de worker.process_one nao sao comprovados pela suite atual; o gate
final real do worker e exercitado diretamente. UI/nonce ficam com o principal.

## Contrato congelado para integracao

```python
control = ReplyControl(repo, registry, external_adapters=None,
                       external_pause_enabled=False)
result = control.ingest({
    'id': 'manual-' + request_id, 'channel': channel, 'content': content,
    'received_at': server_time_iso_with_timezone,
    'hubspot_contact_id': contact_id, 'campaign_id': campaign_id,
    'human_reply': True,
    'source': {'provider': 'manual', 'account_id': 'ygor',
               'provider_message_id': request_id},
    'identity_verified': True,
}, actor='ygor')
view = control.snapshot()
```

O servidor autentica Ygor, exige nonce novo e valida contato exato no snapshot
da campanha (task.contact_ids/activity.contact_ids ou membership atual verificado)
antes de construir esse payload. Nunca encaminhar flags/source do browser.
UI usa crypto.randomUUID, conservando request_id ate sucesso. O primeiro
received_at fica persistido; o replay pode ter outro horario de chegada.
Nao montar ingestao publica de provider. `actor='verified_source'` e reservado
a composicao privada que verificou conta, evento canonico e humanidade.

Schema fechado: exatamente os nove campos acima, source com exatamente tres
campos. `human_reply` aceita somente bool ou null; `identity_verified` bool.
Contato/campanha podem ser null para triagem; caso presentes, contato decimal
positivo ate 30 digitos, campanha `[A-Za-z0-9_-]{1,100}`. IDs/eventos/conta ate
200 caracteres sem controles; conteudo de 1 a 200.000 caracteres; data ISO com
fuso. Canais: email, linkedin, whatsapp, phone, chattie. Providers: manual,
connectsafely, apollo, instantly, hubspot, gmail, chattie.

Para manual, source.account_id='ygor', actor='ygor' e id igual a
source.provider_message_id ou 'manual-'+source.provider_message_id sao
obrigatorios. Para provider, conta deve bater com binding do Registry
para o mesmo provider; evento canonico Instantly usa `provider_id`, que a futura
composicao deve mapear explicitamente para `provider_message_id`. Nenhum alias
ou nome substitui ID. O ator textual e um contrato de codigo confiavel, nao uma
credencial nem verificacao de autenticidade HTTP.

## Retorno e persistencia

`ingest` retorna `{status, reply, duplicate, alerts,
external_pause_enabled:false, external_pauses_enabled:false}`. Status:
`paused_internal` quando encontrou cadencias internas, `reply_recorded` para
humana confirmada sem cadencia correspondente e `triage` para identidade ou
humanidade nao confirmadas. `ReplyControlError.code` expoe erros estaveis:
`reply_event_conflict` para replay divergente e codigos `invalid_reply_*` para
entradas invalidas. Sem semantica HTTP propria.

`snapshot()` retorna `status='internal_only'`, `replies`, `alerts`,
`source={provider:'repository',state_kinds:['reply','cadence','operator_alert']}`,
os dois flags externos falsos e `automatic_reply_reader_connected=false`.
Cada alerta contem title/reason/content, origem, IDs, owner=ygor,
priority=high, due_at=data atual em America/Sao_Paulo e next_action.
Nenhuma classificacao/sugestao gerada pela skill comercial e alegada.

Reply usa ID `reply-control:` + hash da tupla de origem, guardando event_id
original e input_hash. O replay ignora id de envelope e received_at, mas vincula
todos os outros campos. Outra conta/provider/message ID representa outro evento.
Os estados reply, cadencias correspondentes e operator_alert sao gravados na
mesma transacao existente. Um erro reverte a chamada completa. Ingest rejeita
uma transacao externa ja aberta, evitando commit parcial quando o chamador
captura uma excecao: Repository.transaction aninhado nao possui savepoint.

Cadencias elegiveis: draft, ready_for_review, approved, active, blocked,
needs_channel_decision e paused_reply. completed/cancelled/archived e estados
desconhecidos ficam intactos. Match por hubspot_contact_id explicito, person_key
igual ao ID exato ou `hubspot:<ID>`, ou identity existente canonical/actor=ygor,
com records contendo somente um hubspot_contact_id distinto e canonical_person.
Nao infere por nome nem por email isolado. Cadencia com ID explicito divergente
nao herda um match por person_key. Legada sem person_key usa o ID confirmado
para o argumento de correlacao do coordinator, sem criar identity nova.
Engine preserva estados sent/uncertain e demais nao futuros; passos protegidos
por effects sent/uncertain/needs_reconciliation tambem ficam intactos.

## Portas externas e limites

`external_adapters` aceita somente mapping de plataforma para diagnostico local
ja preparado, nunca cliente/callback. Exemplo fake:
`{'apollo': {'status':'failed'}}` gera external_pause_failed. HTTP 202 gera
external_pause_readback_required. Nenhum diagnostico confirma pausa.
`external_pause_enabled=True` e rejeitado por
external_pause_activation_not_authorized. Nem um flag legado habilita transporte.

`external_pause_plan(provider)` e apenas descricao offline. Apollo: operacao
conhecida remove_or_stop_contact_ids com mark_as_finished e exigencia de readback,
sem endpoint/HTTP implementado. Instantly: pausa individual UNVERIFIED;
lead.status nao e usado para escrita. HubSpot/hubspot_seq: unenroll UNVERIFIED,
sem DELETE inventado. Demais providers bloqueados sem capacidade verificada.
`assess_external_pause` transforma diagnosticos em motivo de alerta, nunca em
sucesso externo, inclusive para HTTP 200/202. Nao ha pausa de campanha inteira.

Nao ha cancelamento de tarefa CRM, liberacao de membership, escrita HubSpot,
envio, leitor automatico ou modificacao de configuracao externa. A parada limita
o worker cooperante que reler o mesmo Repository. Chamadas ja em andamento,
outros executores e cadencias criadas depois deste evento nao sao abrangidos.
Nao ha toques.py real, supressao distribuida, provider real nem SLA demonstrado.

## Evidencia local e handoff

Validacao atual pelo principal:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest \
  test_thunder_os_reply_control test_painel_cadencias test_thunder_os_cadence \
  test_thunder_os_campaign_membership test_thunder_os_external_email_import \
  test_thunder_os_campaign_confirmation
```

110 testes PASS em Python 3.14.3, SQLite temporario e rede bloqueada por mocks.
27 sao os testes focados de modulo e painel. Playwright passou em 1440x1000 e
390x844: carga GET-only, uma gravacao interna somente apos submit explicito,
nonce, escaping, preservacao de desconhecido, erro de fonte e sem overflow.

A falha externa testada e diagnostico fake preparado, nao chamada de API.
HTTP->manual->ReplyControl->readback local foi comprovado pelo principal;
revisao independente ainda e necessaria. Canary real pausado retido por escopo.
Nenhuma rede, producao, CRM, commit, push, deploy ou limpeza executada.

Modulo e documento congelados para integracao. Proximos testes do principal
devem preservar os limites de producao e a distincao entre pausa interna e
parada externa nao confirmada.
