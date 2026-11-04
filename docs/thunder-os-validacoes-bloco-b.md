# Thunder OS — validações técnicas do bloco B

Mapeamento de código e documentação local em 04/11/2026. Nenhuma API externa, credencial ou banco foi consultado; testes existentes foram identificados, não executados nesta leitura. O status resume a melhor evidência para cada item, não significa que todos os seus requisitos estejam completos. **Implementado com teste** pode indicar componente parcial; **só simulado / com fakes** indica que a jornada central do item só foi demonstrada em ambiente simulado; **ausente** indica que não foi localizada implementação para a capacidade. Código, teste, deploy e comprovação em fornecedor são evidências distintas.

## B1 — Apollo — só simulado / com fakes

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Leituras parciais e execução da jornada em simulador. | `_ferramentas/thunder_os/apollo_linkedin_preview.py`: `ApolloLinkedInPreview.read`; `apollo_readonly.py`: `search`, `content`; `apollo_sequence_readonly.py`: `sequence_detail`; `apollo_linkedin_simulation.py`: `FakeApollo.read/get/complete`, `Simulation._execute_locked`; teste `test_thunder_os_apollo_linkedin_simulation.py`. | Paginação completa de tarefas, conteúdo real, status de envio, conclusão e pausa com leitura de volta não estão demonstrados na jornada real. | Em sessão futura de leitura autorizada: `GET /api/v1/users/api_profile`, `GET /api/v1/tasks/{id}`, leitura paginada de tarefas e sequência pelo contrato oficial a confirmar; `GET /api/v1/emailer_messages/search` e `POST /api/v1/emailer_messages/get_content` apenas para consulta. Conferir vínculo tarefa–sequência–lead–usuário, cobertura, texto e status. Identificar chamadas de conclusão e pausa e seus endpoints de readback; efeito real de mutação requer teste separado autorizado, não se comprova por GET. |

## B2 — ConnectSafely — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Cliente de leitura de conta, quota e histórico; tratamento parcial de 429. | `connectsafely_client.py`: `accounts`, `verify_identity`, `account_status`, `quota`, `connections`, `sent_invitations`, `relationship`, `conversations`, `messages`, `reconcile`, `_call`, `_quota_allowed`; `test_thunder_os_connectsafely_client.py`, `test_thunder_os_connectsafely_transport.py` com fake 429. | Identidade efetiva, paginação, reset, `Retry-After`, restrição e distinção entre fila e envio não comprovados no fornecedor. | Futuramente ler `GET /linkedin/accounts`, `/linkedin/account/{id}/status`, `/linkedin/account/{id}/quota`, `/linkedin/relationship/{id}/{publicId}`, `/linkedin/conversations` e `/linkedin/conversations/{urn}/messages`, além de convites enviados pelo método `sent_invitations`. Confirmar perfil, quota por conta, janela/reset, vínculo e cobertura; inspecionar cabeçalhos de resposta reais e documentação de consulta por recibo. Verificar se existe leitura por ID após timeout; ausência de recibo conclusivo mantém resultado incerto. |

## Leituras reais de B1 e B2 (04/11/2026, somente leitura)

Leituras feitas pelo Claude no Mac. Não houve escrita, conclusão de tarefa, envio nem alteração de configuração. A fixture `6aea584a2b8c4b0020f93935` continua `scheduled` e intacta. Dados de terceiros (nomes e perfis de convidados) não foram copiados para cá.

### B1 — Apollo

O acesso foi pelo CLI oficial `apollo` 2.2.0, autenticado como o usuário da Thunder via OAuth, em `https://api.apollo.io`.

| Ponto | Resultado observado |
|---|---|
| Detalhe da tarefa (`GET /tasks/{id}`) | A fixture retorna `type=linkedin_step_connect`, `status=scheduled`, `user_id` (dono, igual ao `creator_id`), `contact_id`, `emailer_campaign_id`, `campaign_name`, `campaign_position=1`, `pending_send=false`, `due_at`, `adherence_status=overdue` e **`linkedin_emailer_template.body_text`** (o texto do passo). |
| Correção do registro anterior | O registro anterior dizia que o detalhe não trazia dono nem template. Os dois campos vêm: o dono em `user_id` (o campo `owner_id` não existe) e o texto em `linkedin_emailer_template.body_text`. |
| Confirmação com a chave do painel (VPS `srv1433640`, release `7d5b3d1`) | Pela chave do painel (`x-api-key`, arquivo `0600` de root), `GET /api/v1/tasks/{id}` devolveu 200 com `user_id`, `emailer_campaign_id`, `campaign_position=1` e `linkedin_emailer_template.body_text` igual ao texto da fixture. `POST /api/v1/tasks/search` também devolveu `total_entries=42` e `total_pages=2` e incluiu o template. Só leitura; a chave não foi exibida. |
| O que o código faz hoje | O leitor e o executor do T3 já usam `user_id` como dono. `apollo_linkedin_preview.py` marca sempre `text_unverified` e não lê o template, de propósito: um template não prova o texto final enviado a cada contato. **A lacuna real não é o campo faltar, e sim provar se `body_text` já vem renderizado por contato ou se traz variáveis como `{{first_name}}`.** O texto da fixture é fixo, então não serve para provar isso. É preciso uma tarefa de teste com variável no passo, o que exige criar ou editar uma sequência no Apollo, sem envio. |
| Busca e paginação (`POST /tasks/search`, filtro por `user_id`) | `pagination.total_entries=42` e `total_pages=2`; a página 1 trouxe 25 tarefas, a 2 trouxe 17 e a 3, zero. A cobertura completa é comprovável pela soma das páginas. A busca também traz `linkedin_emailer_template` nas tarefas de LinkedIn. |
| Tarefas de LinkedIn abertas do usuário | Duas: a fixture (sequência `TESTE THUNDER OS - LinkedIn`, `6aea57ca9f01eb000c4d5672`, ativa, 1 passo) e uma `linkedin_step_message` manual (`6aea56153612b300105c51c3`), sem sequência e sem template. Pela regra AL-03, essa segunda deve ficar bloqueada por falta de texto. |
| Conclusão e pausa | O CLI usa `POST /tasks/{id}/complete` (com `note` opcional) e `POST /tasks/{id}/skip`. **Nenhuma das duas foi chamada.** O efeito sobre o relógio da sequência e o readback depois da conclusão continuam sem prova e exigem teste autorizado. |

### B2 — ConnectSafely

O acesso foi pelo MCP da conta da Thunder (conta `6abe5fc087213e2040fb9b1b`, `publicId=ygorbuenobr`, a única conta visível).

| Ponto | Resultado observado |
|---|---|
| Identidade e estado | `GET /linkedin/accounts` e `/linkedin/account/{id}/status`: `status=AVAILABLE`, `enabled=true`, `hasTokens=true`, `apiSeatAllocated=true`, Premium Business, `lastUsed` presente. Esses são os campos que o broker de leitura do T3 espera. |
| Quota de convites | `CONNECT`: janela **semanal**, `limit=200` (o ConnectSafely rejeita com 429 ao atingir), `linkedinLimit=200`, `resetAt` às segundas, 00:00 UTC. **Isso resolve a divergência com os 90 por semana:** o teto efetivo hoje é 200, igual à A1. Mensagens: limite diário de 150 (o do LinkedIn é 200). |
| O que a quota mede | O `used` conta só ações feitas pelo ConnectSafely. Nesta semana, `CONNECT.used=0`, mas a lista de convites enviados mostra convites de 1 hora a 1 dia atrás, feitos por outra via. **A quota não é o total da conta no LinkedIn**: o teto de A1 precisa somar os convites feitos fora do ConnectSafely, ou ser conservador. |
| Recibo do convite | `get-sent-invitations` devolve `invitationId`, `invitationUrn`, `memberUrn`, `publicIdentifier` e `sentAt` só **relativo** ("Sent 1 hour ago"), sem horário absoluto. A paginação usa `nextStartIndex` e termina em `endOfList=true`; uma página vazia com 502/`INVITATIONS_UNAVAILABLE` não indica fim da lista. Dá para confirmar, depois de um timeout, que o convite saiu, buscando pelo `publicIdentifier` do lead; o horário exato não vem. |
| Ainda não comprovado | Cabeçalhos `Retry-After` e reset numa resposta 429 real; se o POST de convite devolve um ID de recibo síncrono; existência de chave só de leitura. A MCP não mostra cabeçalhos HTTP. |

### Consequências para a jornada Apollo + LinkedIn

- A pergunta pendente 1 ("de onde vem o texto do convite") tem resposta técnica provável: do `linkedin_emailer_template.body_text` da própria tarefa, confirmado também pela chave do painel. Antes de usar esse texto, falta provar se as variáveis já vêm renderizadas. A decisão continua com o Ygor.
- A1 pode usar 200 por semana como teto, sem depender da hipótese dos 90; o contador interno precisa considerar convites feitos fora do ConnectSafely.
- O recibo pós-timeout pode ser a presença do `publicIdentifier` em `sent-invitations`, lido até `endOfList`. Sem horário absoluto, o resultado fica `uncertain` sempre que já existia um convite pendente para a mesma pessoa.

## B3 — Webhooks e respostas — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Captura autenticada e deduplicação em componentes específicos. | `connectsafely_webhook.py`: `ConnectSafelyWebhook._parse/accept/process`; `instantly_webhook.py`: `InstantlyCapture.authenticate/capture`, `InstantlyWebhook.handle`; `test_thunder_os_instantly_webhook.py`. | Entrega real, repetição, ordenação, atraso, recuperação, cobertura e classificação de humana versus ausência não estão comprovadas ponta a ponta. | Consultar em leitura a configuração e tipos de eventos registrados de cada provedor, logs e IDs disponíveis; comparar eventos com `conversations/messages` do B2 e histórico do e-mail do B4, datas e paginação. Verificar assinatura, duplicação e lacunas com amostras autorizadas; obter documentação dos eventos de resposta e ausência. Não presumir endpoint de auditoria inexistente. |

## B4 — E-mail — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Sincronização e rascunho Gmail e leitor de recibos. | `inbox_gmail.py`: `GmailInbox.sync/draft`; `email_readback.py`: `GmailReceiptReader.read_message/read_sent_event/readback`; `test_thunder_os_email_readback.py`. | Integração nativa completa, envio, remetente fixo por lead, bounces, limites e Outlook não demonstrados. | Em integração autorizada, ler `GET /v1/status`, `/v1/messages/{id}` e `/v1/threads/{id}` no serviço de readback e comparar identidade da caixa, remetente, IDs, thread e cobertura. Consultar documentação e endpoints de leitura dos provedores escolhidos para credenciais, cotas e bounces; não presumir Gmail/Outlook completos nem executar envio. |

## B5 — CRM — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Cliente HubSpot com leitura, busca, deduplicação, escrita e reconciliação testadas em isolamento. | `hubspot_executor.py`: `HubSpotHTTP.portal/read/_search/dedupe/write`, `HubSpotExecutor.execute/reconcile`; `test_thunder_os_hubspot_tasks.py`. | Associação, negócios, não contato, duplicados e tipos de atividade suportados não estão comprovados para cada CRM/conta real. | Futuramente conferir conta por `GET /account-info/v3/details`; ler `GET /crm/v3/objects/{kind}/{id}` e `/crm/v4/objects/contacts/{id}/associations/{companies|deals}?limit=500`, paginando; busca de duplicados por `POST /crm/v3/objects/{kind}/search` somente leitura. Comparar IDs, associações, histórico, negócios e propriedade de não contato; consultar esquema e tipos de atividade por CRM. Escrita e seu readback exigem validação separada. |

## B6 — Dados e concorrência — só simulado / com fakes

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Simulação de reserva e lock; coordenação social específica e proteção de recibo não verificado. | `apollo_linkedin_simulation.py`: `FakeStore.account_lock`, `Simulation._execute_locked`; `connectsafely_coordination.py`: `SQLiteSocialCoordinator` e coordenador Postgres; `connectsafely_ledger.py`; testes da simulação e dos componentes. | Não comprova reserva durável única empresa + tarefa Apollo + ação no armazenamento oficial, lock entre processos para essa jornada, recuperação e isolamento multiempresa. | Inspecionar esquema e caminhos de escrita reais em ambiente isolado; testar duas conexões/processos, queda entre reserva e recibo, retomada sem reenvio, indisponibilidade do lock e isolamento de duas empresas. Confirmar uma única base oficial por instalação antes de validação operacional. |

## B7 — Pesquisa e IA — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Pesquisa com validação, proveniência e limites de coleta. | `research.py`: `validate_artifact`, `research_is_current`; `research_producer.py`: `PublicResearchProducer.collect`, `BoundedHTTPSFetcher.fetch`; `apollo_research_input.py`: `ApolloResearchInput.prepare/approve/readback`; `test_thunder_os_research.py`, `test_thunder_os_research_producer.py`. | Validades A13 por categoria, custos e teto por campanha, isolamento completo e OAuth Codex como backend automático não comprovados. | Verificar licenças e permissões de fontes e contrato do OAuth para o uso pretendido; em teste isolado medir custo por modelo, conta e campanha, validade por categoria, fonte/data, qualidade e isolamento. Não inferir autorização de backend pela assinatura. |

## B8 — Painel — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Visões de jornada e placar com cobertura e testes de HTTP/UI. | `campaign_scoreboard.py`: `CampaignScoreboard.snapshot` e função `_management`; `journey_readonly.py`: `JourneyReadonly`; `painel_servidor.py`: `Handler`; `service.py`: `ThunderService._authorize_effect_actor`; `test_thunder_os_campaign_scoreboard.py`, `test_thunder_os_journey_http.py`, `test_painel_cadencias_ui.cjs`. | Campos e estados completos, métricas A10, previsão e papéis editáveis com autorização multiempresa efetiva não demonstrados. | Testar localmente rotas com identidades e papéis de duas empresas, inclusive negação no backend; verificar campos, exceções, coortes e denominadores. Em futura leitura do painel ativo, comparar versão carregada e estados com fontes canônicas, sem tratar HTTP 200 como prova da jornada. |

## B9 — n8n — ausente

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Apenas opção documental, sem instalação decidida ou implementação localizada. | `docs/thunder-os-contexto-completo-entire.md`, decisão sobre n8n. | Necessidade, licença e custo não avaliados para o caso concreto. | Se um fluxo justificar o componente, consultar licença oficial para clientes configurando automações por interface própria e estimar infraestrutura e manutenção; sem instalar nem presumir gratuidade. |

## B10 — Infraestrutura — implementado com teste (parcial)

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Contrato de release/rollback e verificações isoladas de recursos e unidades. | `README.md`; `scripts/test_panel_resource_gates.py`; `scripts/provision_instantly_ingress.py`; `scripts/test_connection_units.py`. | Release e executor ativos, restauração real, monitoramento e Cloudflare não foram verificados nesta leitura. | Futuramente, no host correto, ler `systemctl show` de painel e agente, links `current`/`agent-current` e manifestos; comparar PID/release. Inspecionar monitoramento e configuração de DNS/túnel/rotas Cloudflare por chamadas de leitura apropriadas; testar restauração de backup em ambiente isolado e comprovar rollback, sem migrar banco. |

## B11 — Teste final — só simulado / com fakes

| O que existe | Evidência no código | Lacuna | Próxima verificação |
|---|---|---|---|
| Fixture de autoconvite bloqueada e cenários simulados de recibo, incerteza e reserva. | `test_thunder_os_apollo_linkedin_preview.py`: `test_apollo_http_profile_is_canonical_https_and_self_invite_blocked`; `scripts/test_apollo_linkedin_preview_panel.cjs`; `test_thunder_os_apollo_linkedin_simulation.py`. | Não há comprovação aqui de tarefa apropriada, texto exato, envio real ou recibo externo. | Preservar autoconvite; ler tarefa apropriada e vínculo da conexão/conta, preparar texto exato e conferir prévia. Depois de autorização específica para envio, verificar ação no fornecedor e leitura de volta separada da tarefa Apollo. Não executar agora. |

## Ordem recomendada

1. Fechar B1 e B2 em leitura: identidade, tarefa, paginação/cobertura, conta, quota e evidência de envio ou incerteza; manter a fixture intacta.
2. Fechar B6 com reserva durável, lock entre processos e recuperação sem reenvio em testes isolados; conectar as garantias à jornada real.
3. Fechar B3 para detecção de resposta, interrupção local e pausa Apollo com leitura de volta; validar B5 para histórico e reconciliação.
4. Fechar B8 e B10: autorização, exceções e cobertura no painel; runtime, backup restaurável e rollback comprovados.
5. Preparar B11 e obter autorização específica antes do primeiro envio real; confirmar separadamente fornecedor e Apollo.
6. Evoluir B4 e B7 para cadência nativa e pesquisa por cliente; avaliar B9 apenas se houver fluxo auxiliar que justifique.

## Riscos de desenho confirmados (auditoria 04/11)

- **B1 — tarefa Apollo:** a leitura registrada da tarefa gerada por sequência não trouxe proprietário nem template do convite; a conta padrão da campanha é a decisão existente, mas a origem do texto ainda depende de definição e comprovação. Não completar campos por inferência. Evidência: `.specs/features/apollo-linkedin/design.md`, Risks & Concerns.
- **B2/B10 — identidade dos serviços:** units presentes em `runtime/systemd/` declaram `User=root`; a auditoria registrou que o painel ativo também executava como root. Permissões Unix comuns não isolam a credencial do broker de um painel privilegiado. A prova em staging com UIDs separados e dados falsos não demonstra isolamento do serviço real; conferir a identidade efetiva antes de qualquer ativação.
- **B6 — lock e rede:** `.specs/features/thunder-os-v1/campaign-execution-confirmation.md` documenta chamada síncrona ao provedor dentro de `BEGIN IMMEDIATE`; lentidão pode bloquear outros escritores SQLite. Revisar a solução com transações curtas, exclusão por conta e reconciliação de envio incerto, preservando a proteção contra alterações concorrentes. Não foi implementada aqui.
- **B6/B10 — caminho do armazenamento:** `runtime/hermes-linkedin-cadence.example.json` aponta para `/var/lib/thunder-os/painel.db`, enquanto `runtime/hermes-linkedin-cadence.live.example.json` e `runtime/systemd/thunder-os-agent-hermes-ro.conf` apontam para `/root/.hermes/profiles/ygor-personal/painel-dados/painel.db`. São modelos distintos, não prova do caminho efetivo; revalidar a configuração de cada processo e garantir um único armazenamento oficial por instalação antes de operar. A decisão entre SQLite e PostgreSQL está pendente na seção Authority Map da spec V1.
- **B8/B10 — identidade de usuário:** verificações `actor != 'ygor'` em `_ferramentas/painel_servidor.py` restringem fluxos ao usuário fixo e não atendem, por si, aos papéis configuráveis e isolamento multiempresa de A11. Avaliar autorização e segregação por empresa antes de ampliar uso.

Esta seção registra riscos e evidências, não prova correção, implantação ou autorização de ação externa.
