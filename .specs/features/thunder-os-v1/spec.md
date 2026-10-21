# Thunder OS V1 — PRD

**Status:** Fourth bounded correction complete on 2026-09-29; independent re-verification pending

## Problem Statement

O Painel novo já simplifica a leitura do dia, mas ainda não constitui sozinho um produto operacional completo. O trabalho continua distribuído entre arquivos do `brain`, banco de toques, agentes e integrações, sem um contrato único que leve uma intenção humana até um resultado confirmado e novamente visível no Painel.

O Thunder OS V1 transforma `painel.r3xdigital.tech` na interface humana de um sistema novo e independente. O produto coordena o `brain`, Hermes, Claude e Codex sem expor sua complexidade; Thunder Cockpit e Thunder GTM permanecem legados externos e não são fundação, autoridade nem dependência do novo produto.

## Goals

- [ ] Permitir que uma pessoa leiga abra o Painel e identifique em até 30 segundos o que precisa de atenção, por quê e qual é a próxima ação.
- [ ] Completar pelo Painel uma jornada de trabalho desde a oportunidade até o resultado confirmado, sem consulta manual a arquivos, logs ou terminais.
- [ ] Sustentar uma cadência consistente de pontos de contato por pessoa e conta, sem duplicar mensagens e interrompendo automaticamente a sequência quando houver resposta ou bloqueio.
- [ ] Produzir uma leitura ABM útil da conta, do momento empresarial e da oportunidade para a Thunder Mustard antes de sugerir qualquer abordagem.
- [ ] Garantir que toda ação externa seja específica, autorizada, deduplicada e confirmada por recibo ou leitura real do destino.
- [ ] Dar aos agentes uma interface estável para ler contexto, propor trabalho, receber decisões e devolver resultados sem se tornarem autoridades paralelas.
- [ ] Manter falhas técnicas localizadas e recuperáveis, sem bloquear o restante da operação diária.

## Product Boundary

O Thunder OS V1 é composto pelo Painel, pelos contratos do `brain`, pelo armazenamento operacional necessário e pelas interfaces usadas por Hermes, Claude e Codex. O usuário interage com trabalho, decisões e resultados; nomes de agentes, filas internas, logs, MCPs, processos e arquitetura ficam fora da experiência principal.

Sistemas antigos podem ser consultados durante a implementação apenas como referência. Nenhuma capacidade do Thunder Cockpit ou Thunder GTM entra no Thunder OS sem uma necessidade concreta, auditoria isolada e decisão registrada.

## Out of Scope

| Feature | Reason |
| --- | --- |
| Reviver, corrigir ou migrar integralmente Thunder Cockpit ou Thunder GTM | São sistemas legados separados e importariam a complexidade que o novo produto elimina. |
| Depender de `/root/projects/thunder-cockpit`, `/root/projects/thunder-gtm`, `/opt/thunder-cockpit` ou `/opt/thunder-gtm` em runtime | O Thunder OS deve operar e ser implantado independentemente. |
| Autopilot irrestrito de outbound | Primeiro toque, pessoa nova, conteúdo não aprovado, gasto e override de supressão continuam exigindo autorização humana específica; somente ações explicitamente delegadas ao Hermes podem dispensar um novo clique. |
| Envio de e-mail em volume pelo executor do Thunder OS | Apollo, Instantly e demais executores externos mantêm o envio. A V1 admite registrar suas campanhas em lote para visão, parada por resposta e placar; esse registro não autoriza execução pelo Thunder OS nem ativa a campanha externa. |
| SaaS, múltiplas empresas ou múltiplos tenants | A V1 atende a operação interna da Thunder em um único workspace. |
| Aplicativo móvel nativo | A interface web responsiva cobre desktop e celular. |
| Central de logs, observabilidade técnica ou construtor visual de agentes na navegação principal | Diagnóstico existe sob demanda, mas não é trabalho diário do usuário. |
| Migração destrutiva ou limpeza do legado | Preservação e eventual desativação exigem projeto e autorização próprios. |

## Assumptions & Open Questions

| Assumption / decision | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Nome do produto | `Thunder OS`; a interface continua chamada `Painel Thunder` | Evita colisão operacional com o repositório legado `thunder-gtm`. | y |
| Fundação do produto | Painel novo + `brain` + contratos mínimos para agentes e integrações | É a fronteira explicitamente definida pelo Ygor em 2026-09-29. | y |
| Papel do legado | Fora do runtime; consulta e extração pontual somente após auditoria | Reaproveitar subsistemas inteiros recriaria acoplamento e gates excessivos. | y |
| Estratégia de pesquisa | Account-first: entender empresa e momento antes de escolher pessoa e mensagem | Evita personalização superficial baseada apenas no cargo ou em um post isolado. | y |
| Conhecimento da Thunder | Oferta, ICP, método e provas vêm das fontes canônicas do diretório `thunder/`; somente fatos validados podem entrar em copy | A conexão comercial precisa ser verdadeira e não pode inventar capacidade ou case. | y |
| Saída válida da pesquisa | `abordar`, `acompanhar`, `pesquisar mais` ou `não priorizar`, sempre com justificativa | Pesquisa não existe para forçar uma oportunidade onde não há evidência. | n |
| Usuário principal | Ygor e, por extensão, uma pessoa comercial leiga | O Painel deve ser conferível sem conhecimento técnico. | y |
| Modo operacional | Delegação limitada: agentes preparam; Hermes também executa follow-ups já aprovados, curtidas delegadas e escritas internas no HubSpot com credencial, escopo, teto e kill switch | Preserva velocidade sem conceder autoridade implícita. | y |
| Unidade de trabalho | Um cartão representa uma necessidade ou resultado; conta e pessoa fornecem contexto, não uma segunda fila | Mantém o modelo mental já estabelecido no Painel novo. | n |
| Jornada P1 | oportunidade → contexto → cadência → revisão → autorização → execução dos toques → confirmação → resposta ou encerramento | É o menor fluxo que prova prospecção consistente, não apenas uma mensagem isolada. | n |
| Canais da V1 | LinkedIn e e-mail compõem a cadência; HubSpot recebe o espelho; WhatsApp permanece manual | LinkedIn e e-mail são os pontos de contato necessários para testar consistência multicanal, mantendo cada canal independente. | n |
| Campanhas externas em lote | O `CampaignRegistry` aceita `apollo`, `hubspot_seq` e `codex_local`, além de `thunder_os`, `chattie` e `instantly`; registro externo serve para visão, parada por resposta e placar, sem autorização de execução pelo OS | Reutiliza o registro existente sem reimplementar envio em volume; as rotas de execução do OS continuam exigindo plataforma `thunder_os` e a aprovação exata já prevista. Leitura de resultados e parada externa pertencem às tarefas seguintes do plano. | y |
| Aprovação da cadência | Uma aprovação pode autorizar o pacote completo de toques exatos, com destinatário, canal, conteúdo, ordem e intervalo versionados | Reduz confirmações repetidas sem conceder liberdade para alterar ou acrescentar envios. | n |
| Armazenamento | Evoluir as fontes atuais de forma aditiva; uma autoridade explícita por dado; tecnologia decidida após inventário de leitores e escritores | Evita começar por uma reescrita de banco. | n |
| Agentes | Hermes é o operador contínuo; Claude e Codex podem pesquisar, preparar, implementar e verificar mediante o mesmo contrato | Corresponde à forma de trabalho desejada sem expor agentes como telas concorrentes. | y |
| Pedidos livres | Todo pedido feito no Painel vira item rastreável com estado e resultado; não é tratado como chat efêmero | Permite acompanhamento sem criar uma central de processos. | n |
| Falha externa incerta | Nunca repetir automaticamente; marcar `precisa conferir` até reconciliação | Evita duplicar ações quando o provider respondeu de forma ambígua. | y |
| Retenção | Histórico operacional permanece append-only na V1; exclusão não faz parte do produto | Preserva auditoria e evita política destrutiva prematura. | n |
| Diagnóstico | Saúde resumida e orientação humana no Painel; detalhes técnicos ficam em área secundária protegida | Mantém o produto simples sem esconder indisponibilidade real. | y |
| Desempenho percebido | Conteúdo útil inicial em até 2 segundos no ambiente de produção normal; fontes lentas carregam de forma independente | O Painel deve ajudar no dia a dia sem aguardar todas as integrações. | n |
| Verificação final | Um efeito externo real somente será usado no aceite após autorização explícita do Ygor para alvo, canal e conteúdo | O PRD não concede autorização futura de envio ou gasto. | y |

O aceite da Tarefa 1, conforme delimitação do Ygor em 21/10/2026, exige cadastrar no painel somente as campanhas existentes `MX27-MTY`, `MX27-CDMX`, `BR-CADENCIA-V1` e `CHATTIE-01`. `BR-APOLLO-01` e `MX27-INSTANTLY` ficam para quando existirem nas plataformas; não criar registros fictícios. O cadastro real exige prévia conferida e a rota autenticada do painel com `actor=ygor`, nunca escrita direta no banco. Parâmetros factuais ausentes devem ser obtidos, sem inventar regras, versões, contas ou aprovação.

A aba Cadências deve mostrar a união das campanhas cadastradas e das identificadas nas tarefas, sem duplicar o mesmo ID nem deduzir equivalência por nome ou prefixo. Campanha cadastrada sem tarefa reconhecida aparece como "sem tarefas ainda" quando a leitura de tarefas está completa; leitura incompleta permanece explicitamente não verificada. Recibos, aprovação, respostas e reuniões continuam não verificados na Tarefa 1; atribuição de atividades e recibos pertence à Tarefa 2. Preparação local ou registro isolado não substitui o aceite em produção, que depende de aprovação separada do diff e publicação.

**Open questions: none — all unresolved choices use the recorded defaults above.**

## User Stories

### P1: Começar o dia pelo que importa

**User Story**: Como operador, quero abrir o Painel e ver somente o trabalho que precisa de mim agora para começar sem interpretar sistemas ou relatórios.

**Why P1**: Essa é a porta de entrada e a promessa central do produto.

**Acceptance Criteria**:

1. WHEN o usuário autenticado abre o Painel THEN the system SHALL apresentar `Hoje` com itens priorizados, motivo legível e uma próxima ação por item.
2. WHEN não há trabalho pendente THEN the system SHALL mostrar que está tudo em dia e indicar quais eventos farão novos itens aparecerem.
3. IF uma fonte de dados falha THEN the system SHALL manter as demais áreas utilizáveis e mostrar uma orientação curta apenas no contexto afetado.
4. WHILE detalhes técnicos não foram solicitados the system SHALL manter logs, filas, nomes de processos, agentes, MCPs e identificadores internos fora da experiência principal.
5. The system SHALL apresentar o primeiro conteúdo útil em até 2 segundos no smoke de produção com as integrações externas simuladas na latência normal registrada pelo teste.

**Independent Test**: abrir o Painel com trabalho completo, vazio e uma fonte indisponível; confirmar prioridade, isolamento da falha e ausência de linguagem técnica.

### P1: Entender conta, pessoa e histórico

**User Story**: Como operador, quero abrir um item e entender a conta, a pessoa, o que aconteceu e o que está faltando para decidir com segurança.

**Why P1**: Uma ação simples sem contexto suficiente gera erro comercial e retrabalho.

**Acceptance Criteria**:

1. WHEN o usuário abre um item THEN the system SHALL mostrar conta, pessoa, canal, sinais, último toque, histórico relevante, fonte dos fatos e bloqueios conhecidos.
2. WHEN o mesmo registro aparece em mais de uma fonte THEN the system SHALL apresentar uma única identidade reconciliada ou marcar explicitamente que a identidade precisa ser resolvida.
3. IF um fato necessário não tem fonte ou está vencido THEN the system SHALL marcá-lo como insuficiente e impedir que seja apresentado como validado.
4. The system SHALL manter uma única ficha canônica por conta e vincular pessoas e itens sem copiar o histórico para autoridades concorrentes.

**Independent Test**: abrir fixtures com identidade única, duplicada e incompleta; conferir reconciliação, procedência e bloqueio.

### P1: Investigar a conta e formular a oportunidade ABM

**User Story**: Como operador, quero compreender o possível lead, o momento da empresa e a aderência real à Thunder Mustard para decidir se vale abordar e com qual hipótese.

**Why P1**: Sem pesquisa account-first, a cadência apenas escala mensagens genéricas e transforma um sinal isolado em falsa intenção de compra.

**Acceptance Criteria**:

1. WHEN uma conta entra no Thunder OS THEN the system SHALL consolidar identidade empresarial, setor, porte disponível, modelo de negócio, localização, presença digital e fontes consultadas.
2. WHEN há fatos públicos observados nos últimos 90 dias THEN the system SHALL tratá-los como recentes e registrar o momento da empresa com evento, data observada, URL, recência e explicação de por que o fato pode ser comercialmente relevante; a partir do 91º dia o fato fica vencido.
3. WHEN um sinal admite mais de uma interpretação THEN the system SHALL registrar a hipótese de oportunidade e pelo menos uma hipótese alternativa que não pressuponha intenção de compra.
4. WHEN avalia aderência THEN the system SHALL comparar a conta com o ICP vigente da Thunder Mustard e separar `fit`, `momento`, `qualidade dos dados` e `risco` em vez de condensá-los em uma certeza única.
5. WHEN formula uma oportunidade THEN the system SHALL relacionar o problema possível a uma capacidade documentada da Thunder Mustard, indicando a fonte interna vigente usada nessa relação.
6. IF não existe capacidade, prova ou analogia aprovada da Thunder Mustard THEN the system SHALL impedir seu uso na recomendação e na copy.
7. WHEN existe analogia real com um case aprovado THEN the system SHALL apresentar o case como evidência de capacidade, sem prometer repetição do resultado.
8. WHEN a conta possui pessoas identificadas THEN the system SHALL mapear decisor, influenciadores e lacunas do comitê com evidência e nível de confiança, sem tratar cargo como autoridade comprovada.
9. WHEN a pesquisa termina THEN the system SHALL recomendar exatamente um estado entre `abordar`, `acompanhar`, `pesquisar mais` e `não priorizar`, acompanhado de fatos favoráveis, riscos e próxima ação.
10. IF o mínimo de evidência para abordagem não foi atingido THEN the system SHALL permitir somente um rascunho explicitamente marcado com as informações faltantes para revisão humana; despacho delegado pelo Hermes continua bloqueado até a pesquisa canônica estar completa.
11. IF uma consulta exige crédito ou provider pago THEN the system SHALL mostrar custo, finalidade e alvo antes de solicitar autorização específica.
12. The system SHALL preservar cada fato com fonte e data, permitindo atualizar evidência vencida sem apagar a versão usada em uma decisão anterior.

**Independent Test**: pesquisar uma conta com forte aderência, uma conta ambígua e uma conta fora do ICP; verificar fatos, momento, hipótese alternativa, comitê, conexão com a Thunder e recomendação distinta para cada caso.

### P1: Pedir trabalho aos agentes sem conhecer a arquitetura

**User Story**: Como operador, quero pedir pesquisa, preparação ou correção em linguagem comum e acompanhar o resultado no mesmo item.

**Why P1**: Essa é a interface dos agentes e substitui comandos espalhados em chats e terminais.

**Acceptance Criteria**:

1. WHEN o usuário registra um pedido THEN the system SHALL criar exatamente um item rastreável com objetivo, contexto, responsável lógico, estado e horário.
2. WHEN um agente aceita o pedido THEN the system SHALL registrar `em andamento` sem criar um segundo item para a mesma chave idempotente.
3. WHEN um agente conclui ou falha THEN the system SHALL anexar resultado ou motivo acionável ao item original e atualizar sua próxima ação.
4. IF o pedido exige envio, publicação, gasto, exclusão ou outra autoridade ausente THEN the system SHALL interromper antes do efeito e solicitar uma decisão específica.
5. IF o mesmo pedido é reenviado com a mesma chave idempotente THEN the system SHALL devolver o item existente sem duplicar execução.

**Independent Test**: registrar um pedido, repeti-lo, processá-lo com sucesso e simular falta de autorização; confirmar um único item e resultado legível.

### P1: Revisar e autorizar uma ação exata

**User Story**: Como operador, quero revisar o conteúdo e a consequência de uma ação antes de autorizá-la, sem enfrentar vários gates ou confirmações redundantes.

**Why P1**: É a proteção mínima que permite operar sem recriar a burocracia do legado.

**Acceptance Criteria**:

1. WHEN uma ação sensível está pronta THEN the system SHALL mostrar destinatário, canal, conteúdo final, origem, consequência, custo quando houver e uma única decisão principal.
2. WHEN o usuário autoriza THEN the system SHALL vincular a autorização ao alvo, canal, conteúdo versionado e hash apresentados; o Hermes somente pode operar ações previamente delegadas com credencial própria, escopo explícito, teto diário, kill switch e ator auditável `hermes (delegado)`.
3. IF alvo, canal ou conteúdo mudar após a autorização THEN the system SHALL invalidar a autorização anterior e solicitar nova revisão.
4. IF a ação já foi executada ou reservada com a mesma chave de deduplicação THEN the system SHALL bloquear nova autorização e mostrar a evidência encontrada.
5. WHILE a pausa global estiver ativa the system SHALL impedir efeitos externos mesmo que um item antigo esteja aprovado.

**Independent Test**: autorizar um conteúdo, alterar o payload, tentar duplicá-lo e ativar a pausa; confirmar invalidação e bloqueios sem confirmações adicionais.

### P1: Manter uma cadência consistente de contatos

**User Story**: Como operador, quero planejar e acompanhar vários pontos de contato por pessoa para manter consistência sem repetir mensagens, esquecer retornos ou continuar depois de uma resposta.

**Why P1**: Prospecção raramente produz resposta em um único toque; sem cadência o produto vira apenas um gerador de mensagens isoladas.

**Acceptance Criteria**:

1. WHEN uma abordagem é preparada THEN the system SHALL apresentar uma cadência versionada com destinatário, objetivo, ordem, canal, intervalo e conteúdo de cada toque.
2. WHEN o usuário aprova a cadência completa THEN the system SHALL registrar autorização para cada toque exato do pacote sem exigir novas confirmações enquanto destinatário, canal, conteúdo, ordem e intervalo permanecerem iguais.
3. WHEN chega a data de um toque autorizado THEN the system SHALL colocá-lo entre as prioridades do dia e executá-lo somente se identidade, deduplicação, pausa, supressão e estado anterior continuarem válidos.
4. WHEN uma resposta é recebida THEN the system SHALL pausar todos os toques futuros da pessoa antes de propor qualquer próximo passo.
5. IF um toque, canal, destinatário, conteúdo, ordem ou intervalo aprovado for alterado THEN the system SHALL invalidar somente as autorizações futuras afetadas e solicitar revisão do pacote atualizado.
6. IF um toque falha ou fica com resultado incerto THEN the system SHALL bloquear os toques dependentes até resolução, sem avançar silenciosamente a cadência.
7. IF um canal se torna indisponível THEN the system SHALL mostrar a cadência bloqueada e propor uma alternativa para aprovação sem migrar automaticamente para outro canal.
8. The system SHALL mostrar em uma linha do tempo os toques concluídos, o próximo toque, sua data, o canal, o estado e o motivo de qualquer pausa.

**Independent Test**: aprovar uma cadência LinkedIn + e-mail, vencer dois intervalos, receber uma resposta antes do terceiro toque e confirmar execução única, pausa imediata e linha do tempo completa.

### P1: Executar e comprovar o resultado

**User Story**: Como operador, quero que uma ação autorizada seja executada uma vez e que o Painel mostre o que realmente ocorreu.

**Why P1**: Fila, intenção ou resposta HTTP não constituem produto funcional.

**Acceptance Criteria**:

1. WHEN uma ação autorizada entra em execução THEN the system SHALL reservar atomicamente sua chave de deduplicação antes de chamar o destino.
2. WHEN o destino confirma semanticamente o efeito THEN the system SHALL persistir recibo correlacionado e mostrar o resultado no item após nova leitura.
3. IF o destino rejeita a ação THEN the system SHALL registrar falha acionável e oferecer tentativa manual somente para `provider_rejected`, `email_rejected`, `hubspot_rejected` e `channel_not_configured`; timeout, resultado incerto, supressão e falha de autorização nunca entram nessa allowlist.
4. IF o resultado externo é incerto ou ocorre timeout após o envio THEN the system SHALL marcar `precisa conferir` e impedir repetição automática.
5. WHEN uma escrita no HubSpot é concluída THEN the system SHALL reler o registro exato e comparar os campos esperados antes de mostrar sucesso.
6. The system SHALL manter LinkedIn, HubSpot, WhatsApp e e-mail como capacidades independentes, sem uma autorização habilitar outro canal.

**Independent Test**: executar adaptadores fake com sucesso, rejeição e timeout; depois realizar uma única ação real autorizada e comprovar recibo/readback e atualização do Painel.

### P1: Tratar respostas e definir o próximo passo

**User Story**: Como operador, quero ver respostas recebidas, compreender sua classificação e decidir o próximo passo sem procurar conversas em outros sistemas.

**Why P1**: O valor do outbound aparece na resposta e no avanço comercial, não no volume enviado.

**Acceptance Criteria**:

1. WHEN uma resposta nova é recebida THEN the system SHALL vinculá-la à pessoa, conta, canal e toque precedente ou marcar a relação como não resolvida.
2. WHEN um agente classifica uma resposta THEN the system SHALL mostrar a classificação, confiança ou justificativa e permitir correção humana.
3. WHEN o usuário corrige a classificação THEN the system SHALL preservar a classificação anterior como histórico e usar a correção no próximo passo.
4. IF a resposta indicar descadastro, recusa ou risco THEN the system SHALL aplicar a supressão correspondente antes de qualquer nova proposta de contato.
5. WHEN uma reunião ou avanço comercial é confirmado THEN the system SHALL atualizar o histórico operacional e preparar o espelho do HubSpot sem alegar escrita antes do readback.

**Independent Test**: importar respostas positiva, objeção, negativa e não relacionada; corrigir uma classificação e verificar supressão, próximo passo e histórico.

### P1: Aprender sem alterar regras sozinho

**User Story**: Como operador, quero receber uma recomendação útil baseada em evidência e decidir se ela deve ser testada, ignorada ou entendida melhor.

**Why P1**: O sistema precisa evoluir sem permitir que um agente transforme correlação em regra operacional.

**Acceptance Criteria**:

1. WHEN há evidência suficiente segundo um critério versionado THEN the system SHALL mostrar no máximo uma recomendação com hipótese, população, período, comparação, limitação e ação proposta.
2. WHEN o usuário escolhe `Testar` THEN the system SHALL criar um experimento delimitado com hipótese, métrica, população, responsável, condição de parada e data de avaliação.
3. IF a evidência é insuficiente THEN the system SHALL manter a observação fora das recomendações acionáveis.
4. IF um experimento termina THEN the system SHALL apresentar seu resultado para decisão humana antes de alterar regra, playbook ou automação.
5. The system SHALL impedir que recomendações ou classificadores externos autorizem efeitos, gastos ou mudanças de regra; a exceção operacional é o Hermes autenticado com credencial própria, que pode executar somente follow-ups de uma cadência já aprovada, curtidas previamente delegadas e escritas internas no HubSpot, respeitando teto diário e kill switch. Primeiro toque, pessoa nova e qualquer override de supressão continuam exclusivos do humano.

**Independent Test**: fornecer evidência abaixo e acima do limiar, iniciar experimento e encerrá-lo; confirmar que nenhuma regra muda sem decisão humana.

### P1: Operar com segurança e recuperação simples

**User Story**: Como operador, quero confiar que o produto preserva o trabalho, falha de maneira compreensível e pode voltar para a versão anterior.

**Why P1**: Produto pronto inclui operação, não apenas telas e código local.

**Acceptance Criteria**:

1. IF o armazenamento obrigatório está indisponível THEN the system SHALL falhar fechado para escritas e manter leituras comprovadamente seguras disponíveis.
2. IF duas execuções disputam o mesmo item THEN the system SHALL permitir no máximo uma transição válida e rejeitar a concorrente sem perder histórico.
3. WHEN o serviço reinicia THEN the system SHALL reconstruir itens pendentes e estados confirmados a partir das fontes persistentes sem duplicar efeitos.
4. WHEN uma versão é promovida THEN the system SHALL registrar revisão de fonte, artefato, configuração efetiva, migrações, verificação e rollback.
5. IF a verificação de produção falha THEN the system SHALL preservar ou restaurar a última versão conhecida como boa sem editar a release publicada.
6. The system SHALL proteger o Painel por autenticação e limitar interfaces de agente e escrita a identidades explicitamente autorizadas.
7. The system SHALL registrar eventos operacionais estruturados sem expor segredos, conteúdo sensível ou logs técnicos na navegação principal.

**Independent Test**: simular indisponibilidade, concorrência e reinício; promover um candidato isolado e exercitar rollback com evidência de revisão.

## Edge Cases

- IF uma pessoa não tem identidade suficiente THEN the system SHALL bloquear ação externa e pedir resolução de identidade.
- IF o provider retorna sucesso sem identificador ou readback suficiente THEN the system SHALL manter o resultado como `precisa conferir`.
- IF a autorização expira antes da execução THEN the system SHALL voltar o item para revisão sem chamar o provider.
- IF uma resposta e um toque agendado chegam concorrentemente THEN the system SHALL aplicar a resposta primeiro e impedir o toque futuro antes de qualquer chamada ao provider.
- IF um agente devolve resultado fora do contrato THEN the system SHALL rejeitar a atualização, preservar o item e registrar o erro sanitizado.
- IF um arquivo sincronizado conflita com estado operacional mais recente THEN the system SHALL preservar ambas as versões e encaminhar reconciliação sem sobrescrita silenciosa.
- WHEN o volume de itens supera o limite da abertura THEN the system SHALL manter a prioridade determinística e oferecer acesso ao restante sem carregar tudo na primeira dobra.
- IF o serviço de IA ou classificador está indisponível THEN the system SHALL preservar operação manual e não bloquear leitura, aprovação ou reconciliação.

## Implicit Requirements Coverage

| Dimension | Resolution |
| --- | --- |
| Input validation & bounds | Todo contrato de escrita valida identidade, formato, tamanho, estado anterior e campos permitidos; limites concretos serão fechados no design a partir dos adaptadores reais. |
| Failure / partial-failure states | Falhas são locais; efeitos incertos entram em reconciliação e nunca em repetição automática. |
| Idempotency / retry / duplicate handling | Pedido, autorização e efeito têm chaves estáveis; retry externo é manual na V1. |
| Auth boundaries & rate limits | Painel autenticado; interfaces de agentes com identidade própria; limites dos providers são tratados por adaptador. |
| Concurrency / ordering | Transições usam comparação de versão ou reserva atômica; somente uma execução possui o item. |
| Data lifecycle / expiry | Histórico append-only na V1; autorizações expiram; política de exclusão está fora do escopo. |
| Observability | Eventos estruturados, saúde resumida e correlação por item; detalhes técnicos fora da interface principal. |
| External-dependency failure | Timeouts, circuit breaker simples quando necessário e reconciliação antes de retry. |
| State-transition integrity | Estados e transições permitidas serão contrato único; UI e agentes não podem escrever estados arbitrários. |

## Requirement Traceability

| Requirement ID | Story | Phase | Status |
| --- | --- | --- | --- |
| TOS-01 | Começar o dia | Validation | 5/5 PASS no contrato local; produção não executada |
| TOS-02 | Contexto de conta e pessoa | Re-validation | Correção e testes produtor→consumidor prontos; verificação independente pendente |
| TOS-03 | Pesquisa e oportunidade ABM | Re-validation | Regra de 90 dias e rascunho incompleto governado prontos; verificação independente pendente |
| TOS-04 | Pedidos aos agentes | Validation | 5/5 PASS |
| TOS-05 | Revisão e autorização | Re-validation | Delegação autenticada e limitada pronta; verificação independente pendente |
| TOS-06 | Cadência multitoque | Re-validation | Readback de toque devido pronto; verificação independente pendente |
| TOS-07 | Execução e resultado | Re-validation | Allowlist de retry definida e testada; verificação independente pendente |
| TOS-08 | Respostas e próximo passo | Re-validation | Supressão persistente após correção pronta; verificação independente pendente |
| TOS-09 | Aprendizado governado | Re-validation | Experimento completo e fronteira Hermes prontos; verificação independente pendente |
| TOS-10 | Segurança e recuperação | Re-validation | Rollback canônico executável/testado; produção não executada |

**Coverage:** 50/62 critérios PASS na terceira verificação independente; a feature não está pronta para promoção.

## Success Criteria

- [ ] Uma pessoa sem conhecimento da arquitetura conclui o roteiro diário de aceite sem abrir terminal, arquivo Markdown, log ou sistema legado.
- [ ] Para cada conta priorizada, o Painel explica com fontes por que agora, qual oportunidade é apenas hipótese, quem pode participar da decisão e qual capacidade comprovada da Thunder sustenta a abordagem.
- [ ] Uma jornada autorizada percorre oportunidade, contexto, proposta, autorização, efeito, recibo/readback e atualização visível no Painel.
- [ ] Uma cadência multicanal autorizada executa os toques uma única vez, respeita intervalos e pausa todos os próximos contatos quando chega uma resposta.
- [ ] Os testes demonstram que payload alterado, duplicidade, pausa global, timeout incerto e transição concorrente não produzem efeito duplicado.
- [ ] O Painel permanece útil quando uma integração ou serviço de IA está indisponível.
- [ ] Nenhum processo do Thunder Cockpit ou Thunder GTM é requisito de runtime do Thunder OS.
- [ ] O release final possui revisão identificável, verificação de produção e rollback exercitável.
