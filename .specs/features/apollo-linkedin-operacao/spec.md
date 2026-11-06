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
| Orquestração | Apollo é o relógio da sequência única; Thunder processa exclusivamente as tarefas LinkedIn agendadas | Decisão de Ygor de 06/11; os passos são lineares, com salto condicional pelo executor | y |
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

### P3: sequência única com Apollo como relógio

**User Story**: Como gestor, quero que o Apollo agende e-mails e tarefas LinkedIn na mesma sequência, e que o Thunder OS processe a tarefa no momento definido sem disparar mensagem para quem não está conectado.

1. WHEN o Apollo agendar uma tarefa LinkedIn aberta para campanha aprovada THEN o executor SHALL vinculá-la à empresa, contato e conta dona e processá-la com os controles existentes; ele não cria calendário independente. (AO-23)
2. WHEN a tarefa for `linkedin_step_connect` THEN o executor SHALL usar a nota renderizada da própria tarefa, validar texto e hash, e exigir recibo confirmado antes de concluir no Apollo. Convite enviado não significa aceite. (AO-24)
3. WHEN uma tarefa `linkedin_step_message` estiver aberta e a relação verificada na conta dona for `CONNECTED` THEN o executor SHALL seguir o fluxo existente de envio aprovado, recibo e conclusão. Aceite não é resposta nem prova interesse. (AO-25)
4. WHEN a mesma leitura confiável da conta dona comprovar relação diferente de `CONNECTED` THEN o executor SHALL pular a tarefa no Apollo com a nota `não conectado, pulado`, sem executar POST de mensagem LinkedIn. (AO-26)
5. IF a relação estiver desconhecida, incompleta, vencida, contraditória, ou vinculada a outra conta/perfil THEN o executor SHALL bloquear visivelmente e não pular nem enviar; uma falha de leitura não é prova de ausência de conexão. (AO-27)
6. WHEN um template exigir personalização ABM THEN a frase SHALL vir do campo customizado de contato configurado e renderizado pelo Apollo; antes de agir, validar presença do valor obrigatório e bloquear texto vazio ou com `{{` ou `}}` não resolvidos. Renderização de campo ausente para uma string parcialmente preenchida exige comprovação separada do campo; não inferir valor não vazio só porque o restante do texto existe. (AO-28)
7. IF houver resposta humana em fonte obrigatória THEN o sistema SHALL bloquear os próximos toques, registrar urgência e histórico e permitir sugestão sujeita a revisão humana, conforme AO-13–22. (AO-29)
8. IF a leitura, a operação de salto ou sua releitura falhar THEN o sistema SHALL exibir estado pendente/indeterminado e não apresentar como concluída por inferência nem emitir uma mensagem LinkedIn. (AO-30)
9. WHEN o Apollo confirmar salto THEN o resultado SHALL ser identificado como pulado, nunca como mensagem entregue; idempotência e reconciliação não geram POST LinkedIn adicional. (AO-31)
10. WHILE a chave de execução estiver desligada ou o teste usar provedores simulados THEN SHALL não haver conclusão/salto real no Apollo, envio, ativação ou timer. (AO-32)

Exemplo linear: D0 e-mail 1 e convite; D3 e-mail 2; D6 mensagem LinkedIn; D9 e-mail 3; D12 mensagem LinkedIn; D14 e-mail final. Os dias são intenção de configuração, não garantia de datas absolutas: intervalos do Apollo contam a partir do passo anterior e podem ser afetados por tarefas pendentes e janela de envio. A documentação do Apollo informa que uma tarefa pendente segura o avanço até conclusão, salto ou auto-salto; o comportamento específico com a sequência e a API desta instalação ainda requer teste real com contato de teste pelo Claude, sem envio. Um convite aceito antes do D6 não gera mensagem imediata por si só: o worker aguarda a tarefa Apollo correspondente.

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
| AO-23–32 | relógio Apollo, salto de tarefa LinkedIn e personalização | tasks | pending |
