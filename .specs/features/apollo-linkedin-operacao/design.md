# Desenho: Apollo + LinkedIn e interrupção por resposta

## Fronteira e autoridades

Apollo controla os passos e e-mails da sequência; ConnectSafely executa as ações LinkedIn; o SQLite oficial do Thunder OS guarda aprovação, vínculos, reservas, eventos e estado de reconciliação. CRM é memória comercial e fonte de resposta quando configurado, não a autoridade da reserva. Somente um armazenamento oficial por instalação; nenhuma solução promete deduplicação entre bases independentes. Uma ação tem identidade única `(empresa, id_tarefa_apollo, tipo_ação)`; conta, destinatário, versão e hash são vínculos obrigatórios, não substitutos dessa chave.

Reutilizar o leitor Apollo, CampaignRegistry/ApprovalPack, `apollo_linkedin_live_executor`, `MessageAdapter`, `ReplyCoverage`, reservas e lock do Repository, cliente ConnectSafely e `reply_control`; estender o fluxo existente antes de criar outro serviço ou tabela. O resultado comprovado da mensagem T4d, testes com fakes e runtime são evidências separadas. A conta padrão é usada quando não houver indicação mais específica válida; após conexão prevalece a conta dona (A3). Sem acesso à conta dona, bloquear, nunca alternar silenciosamente. O padrão inicial de saldo semanal/rodízio aplica-se à escolha autorizada de lead novo e não autoriza trocar a conta já dona.

## Jornada e fluxo

1. Gestor/admin aprova campanha, versão, sequência, donos, conta e regras uma vez; o processamento deve rejeitar aprovação inválida, expirada ou versão divergente. A7 preserva versão de quem já começou mediante prova durável de matrícula.
2. Um runner explicitamente configurado consulta páginas Apollo com limites de tempo/páginas; sem habilitação, opera somente em prévia. O agendador e os serviços reais permanecem desativados até autorização aplicável. Coleta incompleta não exclui histórico e nunca prova ausência de tarefas/respostas.
3. Identificar `linkedin_step_message` ou `linkedin_step_connect`, task/lead/empresa/campanha, texto renderizado/hash, conta e fontes. Conta, quota, limitação por semana e espaçamento, saúde, calendário e janela devem ser conhecidos; estados desconhecidos produzem bloqueio visível. Períodos 40–90 s dizem respeito a ações da conta; não substituem o intervalo mínimo de um dia útil por lead de A2.
4. Obter lock por conta e realizar reserva atômica antes de chamada. Relê tarefa aberta, conteúdo, aprovação, chave, pausa e cobertura imediatamente antes do POST. Uma ação incierta nunca volta automaticamente para envio. O runner respeita outros caminhos e limites de conta existentes; não basta testar um boolean informado pelo chamador.
5. Mensagem exige conexão e recibo identificável, ou leitura que prove conta, destinatário, conteúdo e instante posterior à reserva. Convite usa evidência de envio específica do ConnectSafely com paginação completa e conflito prévio tratado como incerto. Não confundir aceitação do convite com resposta humana. Não presumir unicidade temporal se histórico só fornecer data relativa.
6. Só após recibo durável, e sem resposta concorrente, iniciar conclusão Apollo; falha/timeout vira `apollo_reconcile_pending`. A recuperação consulta/reconcilia apenas Apollo e nunca repete LinkedIn. Uma tarefa removida ou desativada não é conclusão; 404/422 em detalhe gera `task_removed`, em busca sinaliza cobertura desconhecida.
7. Observações de Apollo e ConnectSafely e do CRM quando configurado levam identificação, escopo, hora, cobertura e expiração. Com resposta humana, persistir parada e evento antes de novo envio, propagar bloqueio aos contatos elegíveis da empresa prospectada, abrir urgência e tentar pausa da matrícula Apollo. Se ainda não comprovada, exibir `local_paused_only`/`external_pause_pending`; somente leitura de volta autoriza `external_pause_confirmed`. Falha externa nunca desfaz parada local.
8. Durante envio, uma resposta concorrente exige preservar recibo se houver, manter tarefa Apollo não concluída e abrir revisão; a origem e a serialização devem ser testadas com uma barreira controlada. O operador classifica interesse/retorno/objeção/não contato; nenhuma resposta humana retoma automaticamente. O sistema pode sugerir texto, mas humano revisa e autoriza envio.

## Integrações e evidência

- Usar APIs/adaptadores oficiais já comprovados; capacidade de pausar matrícula específica, permissões e forma de releitura não estão presumidas. Investigar documentação oficial e fornecer fakes representativos; se não houver recurso confiável, manter pausa local e pendência visível.
- A cobertura Apollo por ID pode não conter campos necessários: campos ausentes significam `unknown`, nunca ausência de resposta. LinkedIn requer histórico da conversa por conta e destinatário, com paginação/frescor verificáveis; não alterar a regra `empty tail` validada para ConnectSafely sem evidência contrária.
- CRM é obrigatório apenas quando configurado e aplicável à identidade. Sem e-mail, contato só LinkedIn pode usar Apollo por contact_id; CRM não configurado é `not_configured`, não `unknown`. Provider indisponível ou identidade ambígua bloqueia quando a fonte é obrigatória.
- A ficha/alerta deve incluir o que foi realmente observado, timestamps e cobertura. Alertas imediatos por canal transacional separado seguem A9/A14; o envio de resposta comercial continua sob revisão humana.

## Estados observáveis

| Estado | Evidência e ação seguinte |
| --- | --- |
| `ready` | Tarefa, aprovação, conta, texto e cobertura validados; ainda não é recibo. |
| `reserved` | Reserva durável; não indica envio. Reserva órfã aparece em atenção. |
| `confirmed` | Recibo durável do fornecedor; conclusão Apollo é estado separado. |
| `uncertain` | Efeito externo possível sem prova; sem retry automático, resolução humana auditada. |
| `apollo_reconcile_pending` | LinkedIn confirmado e Apollo sem confirmação; reconciliação só Apollo. |
| `task_removed` | Apollo informou ausência/remoção; nunca concluída por inferência. |
| `local_paused_only` / `external_pause_pending` | Parada local persistida, pausa Apollo ainda não comprovada; urgente e bloqueante. |
| `external_pause_confirmed` | Pausa Apollo demonstrada em releitura para a mesma matrícula. |
| `reply_needs_review` | Resposta humana com contexto e classificação/sugestão para operador; sem retomada automática. |

## Limites e prova

Testes de unidade/contrato, corrida entre processos na mesma base temporária, condições de resposta em voo e integração HTTP falsa com zero rede. Não usar dados reais em fixtures nem alterar a fixture protegida. Uma execução bem-sucedida com mocks não prova quota, permissões, estado do serviço ou comportamento do fornecedor real. Prova de produção depende de implantação reversível e envio real separado, expressamente autorizado por Ygor; um estado de fila não é recibo. Respeitar isolamento por empresa nas fronteiras já suportadas, sem alegar que plataforma multiempresa está concluída.

## Contrato pendente: vínculo HubSpot por empresa

Quando HubSpot estiver configurado na campanha, a cobertura de resposta deve relacionar a aprovação e a empresa do Thunder OS ao portal verificado pela própria credencial, e ao contato e empresa HubSpot obtidos por identidade e associação únicas. O vínculo deve conter IDs estáveis, hash da evidência, data e escopo de campanha, sem transportar credenciais. Antes de agir, releituras devem confirmar o mesmo portal, identidade, associação e frescor; divergência, ambiguidade, cobertura parcial ou ausência de evidência retornam `unknown` e bloqueiam o envio. CRM não configurado permanece explícito.

O leitor de elegibilidade existente fornece `candidate()` e hash de identidade, mas os leitores de elegibilidade e de histórico estão presos ao portal/owner da instalação atual. Parametrizá-los e associar portal à credencial autorizada é pré-requisito para múltiplas empresas; `provider_accounts` como string, isoladamente, não comprova essa associação. O vínculo antigo de sincronização Apollo–HubSpot também não prova vínculo da empresa. Não introduzir wrapper que diga suportar outros tenants enquanto essas dependências não forem resolvidas.

O histórico HubSpot disponível informa direção e data, mas não classifica autoria humana. A detecção deve comparar a resposta ao marco verificável do último toque da campanha, conservar a evidência original e tratar autor automático ou desconhecido como classificação pendente, com bloqueio conservador. Não transformar uma resposta antiga em evento novo nem rotular automaticamente uma resposta humana sem evidência. Quando houver eventos de histórico já registrados, preservar sua proveniência e cobertura.
