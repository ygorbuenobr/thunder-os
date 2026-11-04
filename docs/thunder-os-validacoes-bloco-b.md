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
