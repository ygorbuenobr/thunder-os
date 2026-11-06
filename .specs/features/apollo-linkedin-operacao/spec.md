# Apollo + LinkedIn: operação e respostas

## Problem Statement

O gestor quer coordenar sequências de e-mail já existentes no Apollo com tarefas de convite e mensagem no LinkedIn. Um serviço do Thunder OS deve processar tarefas aprovadas e interromper a prospecção quando houver resposta, sem duplicar ações, concluir prematuramente uma tarefa ou afirmar pausa externa sem confirmação. O operador acompanha o contexto e as exceções no painel. O B2 de mensagem pelo executor foi confirmado em produção, com recibo, releitura LinkedIn e conclusão Apollo, conforme `docs/thunder-os-validacoes-bloco-b.md` no commit `6bc259d`; isso não comprova convites nem execução contínua.

## Out of Scope

| Item | Razão |
| --- | --- |
| Construtor genérico de cadência nativa e aquisição/importação de leads | Jornadas posteriores; este recorte usa sequência do Apollo existente. |
| Automatizar resposta comercial ou envio de mensagem sem autorização | Ygor exige revisão humana de respostas e proíbe envio real sem autorização. |
| Migrar banco, trocar provedor, implementar multiempresa completo | Uma base oficial por instalação; capacidades gerais seguem fases posteriores. |
| Ativar serviço, timer ou envio em produção durante implementação | O usuário autorizou código e testes, não novos envios. |

## Assumptions & Open Questions

| Assumption / decision | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Orquestração | Apollo dono da sequência e e-mail; Thunder processa tarefas LinkedIn | Decisão de produto | y |
| Autoridade de envio | Campanha e regras aprovadas uma vez; envio real desta rodada exige autorização explícita | Decisão de Ygor e pedido atual | y |
| Armazenamento e concorrência | Um SQLite oficial por instalação; chave empresa+tarefa+ação e lock por conta | Decisão 1–3 do usuário | y |
| Dono da conta | Conta padrão se tarefa não indicar outra; após conexão conserva conta dona; indisponível bloqueia | Decisões de campanha e A3 | y |
| Texto | `linkedin_emailer_template.body_text` renderizado, relido e comparado por hash | Evidência B1/B2, decisão de 05/11 | y |
| Limites e ritmo | Menor limite configurado/provedor; referência 200 convites por perfil/semana; intervalo configurável de 40–90 s entre ações, além das regras A2 | Decisões A1/A2 e Ygor | y |
| Fontes de resposta | Apollo e ConnectSafely obrigatórios; CRM configurado participa quando identidade e cobertura forem demonstráveis | Decisão multicanal; ausência configurada é explícita | y |
| Pausa Apollo | Bloqueio local imediato; estado externo confirmado somente após escrita e releitura comprovadas | R11 e A6 | y |
| Implementação de pausa externa | Usar somente mecanismo/API efetivamente documentado e validado; falha ou impossibilidade fica visível como pendente | Não inventar capacidade do fornecedor | y |
| Resolução de resultado incerto | Sem retry automático; gestor/admin resolve com justificativa; nenhuma resolução implica envio | A11 e decisões anteriores | y |

Open questions: capacidades reais de pausa por matrícula e sinais de resposta por fonte exigem evidência técnica; até comprovadas são limitações visíveis, não novas decisões de produto. A confirmação do envio real relatado por outra sessão não autoriza novos envios.

Readback T5 informado em 06/11: comparar a mesma matrícula por contato e sequência; uma membership antes `active` com `inactive_reason` vazio deve deixar de estar ativa ou ganhar um `inactive_reason` explícito após `stop`. O contrato permanece não homologado até ensaio no contato de teste em produção com releitura, sem disparar mensagens.

## User Stories

### P1: processar tarefas da sequência

**User Story**: Como gestor, quero aprovar a campanha e deixar o serviço identificar e tratar tarefas de LinkedIn com a conta certa, acompanhando o resultado no painel.

**Acceptance Criteria**:

1. WHEN uma campanha Apollo aprovada tem tarefa de mensagem ou convite aberta THEN o serviço SHALL identificá-la e vinculá-la à empresa, sequência, dono, contato e conta autorizados. (AO-01)
2. IF nenhuma tarefa elegível for encontrada THEN o serviço SHALL relatar coleta vazia com cobertura e horário, sem criar reserva nem enviar. (AO-02)
3. IF o armazenamento oficial, aprovação vigente, vínculo, texto válido, conta ou cobertura necessária não estiver comprovado THEN o serviço SHALL bloquear e mostrar o motivo sem enviar. (AO-03)
4. WHEN uma tarefa elegível for processada THEN o serviço SHALL reservar atomicamente a chave empresa+tarefa+ação e obter o lock da conta antes de qualquer efeito. (AO-04)
5. IF dois processos ou leituras repetidas disputarem a mesma chave THEN o sistema SHALL permitir no máximo um envio e registrar resultado observável. (AO-05)
6. WHEN a ação estiver prestes a ser enviada THEN o executor SHALL reler a tarefa Apollo e os portões, recusar status fechado/removido e texto alterado, e nunca usar outra conta silenciosamente. (AO-06)
7. WHEN uma mensagem for enviada THEN o sistema SHALL aceitar apenas recibo associado à conta, destinatário e conteúdo ou releitura que prove a mesma mensagem depois da reserva. (AO-07)
8. WHEN um convite for enviado THEN o sistema SHALL exigir confirmação de envio vinculada à conta e ao destinatário, com prova completa adequada ao fornecedor antes de concluir Apollo. (AO-08)
9. IF a ação tiver resultado incerto, convite prévio ambíguo ou prova incompleta THEN o sistema SHALL preservar `uncertain`, bloquear repetição automática e manter tarefa Apollo não concluída. (AO-09)
10. WHEN houver recibo durável e nenhuma resposta concorrente impeditiva THEN o sistema SHALL tentar concluir Apollo e reconciliar somente Apollo após falha, sem repetir o LinkedIn. (AO-10)
11. IF os limites, intervalo por conta, janela, calendário ou saúde não forem atendidos THEN o serviço SHALL adiar sem rajada e informar motivo e próxima verificação conhecida. (AO-11)
12. WHILE a chave de execução estiver desligada THEN o sistema SHALL apenas ler e preparar, sem executar LinkedIn ou concluir tarefa Apollo. (AO-12)

### P2: interromper ao receber resposta

**User Story**: Como operador, quero que uma resposta interrompa a prospecção e apareça com o histórico, para responder pessoalmente.

**Acceptance Criteria**:

1. WHEN uma resposta humana for observada em Apollo, LinkedIn ou CRM configurado com identidade e cobertura verificadas THEN o sistema SHALL persistir o evento e bloquear imediatamente os próximos envios da matrícula e canais relacionados. (AO-13)
2. WHEN o contato pertencer à mesma empresa prospectada de outros contatos em prospecção THEN o sistema SHALL bloquear também esses contatos conforme A12, sem presumir vínculo desconhecido. (AO-14)
3. IF qualquer fonte obrigatória estiver `unknown`, incompleta ou desatualizada THEN o sistema SHALL impedir novos envios e exibir cobertura/fonte/data; ausência de CRM não configurado SHALL ser identificada como tal. (AO-15)
4. WHEN houver resposta THEN o sistema SHALL solicitar a pausa da matrícula Apollo e exibir `local_paused_only` até releitura provar a pausa externa. (AO-16)
5. IF resposta chegar durante envio THEN o sistema SHALL preservar eventual recibo, não concluir automaticamente a tarefa Apollo e criar revisão humana. (AO-17)
6. IF a pausa Apollo falhar ou não puder ser comprovada THEN o sistema SHALL manter bloqueio local, indicar `external_pause_pending` e reconciliar sem reenviar LinkedIn. (AO-18)
7. WHEN a pausa externa for confirmada por releitura THEN o sistema SHALL exibir `external_pause_confirmed` com fonte e data. (AO-19)
8. WHEN uma resposta for registrada THEN o painel SHALL criar item urgente com histórico disponível e permitir sugestão de resposta, mantendo envio e classificação sob controle humano. (AO-20)
9. IF a resposta for humana THEN o sistema SHALL nunca retomar automaticamente; somente pessoa autorizada pode classificar ou resolver conforme A5/A6/A11. (AO-21)
10. IF leitura for parcial THEN o sistema SHALL preservar histórico já observado e distinguir coorte atual de histórico preservado, sem inventar remoções. (AO-22)

## Requirement Traceability

| Requirement ID | Jornada | Phase | Status |
| --- | --- | --- | --- |
| AO-01 | identidade e seleção | tasks | pending |
| AO-02 | coleta vazia | tasks | pending |
| AO-03 | bloqueios | tasks | pending |
| AO-04 | reserva e lock | tasks | pending |
| AO-05 | idempotência | tasks | pending |
| AO-06 | releitura | tasks | pending |
| AO-07 | recibo mensagem | tasks | pending |
| AO-08 | recibo convite | tasks | pending |
| AO-09 | incerto | tasks | pending |
| AO-10 | conclusão | tasks | pending |
| AO-11 | ritmo | tasks | pending |
| AO-12 | chave desligada | tasks | pending |
| AO-13 | bloqueio local | tasks | pending |
| AO-14 | empresa prospectada | tasks | pending |
| AO-15 | cobertura | tasks | pending |
| AO-16 | pausa externa | tasks | pending |
| AO-17 | resposta durante envio | tasks | pending |
| AO-18 | pausa pendente | tasks | pending |
| AO-19 | pausa comprovada | tasks | pending |
| AO-20 | urgência e contexto | tasks | pending |
| AO-21 | controle humano | tasks | pending |
| AO-22 | histórico | tasks | pending |
