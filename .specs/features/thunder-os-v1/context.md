# Thunder OS V1 — Contexto

**Gathered:** 2026-09-29
**Spec:** `.specs/features/thunder-os-v1/spec.md`
**Status:** Approved for design

## Feature Boundary

O Thunder OS V1 transforma o Painel novo em produto operacional completo. Ele usa o `brain` e contratos mínimos para Hermes, Claude, Codex e integrações. Thunder Cockpit e Thunder GTM são legados separados e não participam do runtime nem governam a arquitetura nova.

## Implementation Decisions

### Identidade do produto

- O produto se chama `Thunder OS`.
- A interface humana se chama `Painel Thunder` e permanece em `painel.r3xdigital.tech`.
- “Thunder GTM OS” não será usado como nome técnico porque colide com o legado `thunder-gtm`.

### Experiência humana

- A abertura responde “o que precisa de mim agora?”.
- Uma pessoa leiga deve conferir contexto, decidir e acompanhar o resultado.
- Logs, filas, MCPs, agentes e arquitetura não aparecem na navegação principal.
- Falhas técnicas aparecem como orientação localizada, sem códigos ou stack traces.

### Agentes

- Hermes é o operador contínuo do sistema.
- Claude e Codex colaboram em pesquisa, preparação, engenharia e verificação.
- O Painel mostra trabalho e resultado, não uma central de agentes.
- Todos os agentes usam o mesmo contrato de item, contexto, decisão e resultado.
- Hermes usa credencial própria e delegação explícita. O escopo inicial permite apenas follow-up de cadência já aprovada, curtidas delegadas e escrita interna no HubSpot. Primeiro toque, pessoa nova e override de supressão continuam humanos.
- A delegação possui teto diário, kill switch e auditoria com ator `hermes (delegado)`.

### Segurança proporcional

- Não haverá transplante do conjunto de gates do legado.
- As proteções essenciais são autorização específica, deduplicação, pausa global, recibo/readback e reconciliação de resultado incerto.
- Uma única confirmação humana deve bastar para a ação exata apresentada.
- Autorização para um canal ou payload nunca se propaga para outro.

### Cadência

- O produto não termina na primeira mensagem; cada pessoa pode ter uma sequência coerente de pontos de contato.
- A cadência combina LinkedIn e e-mail, mantendo autorização, deduplicação e estado separados por canal.
- O usuário revisa o pacote completo de toques em uma única experiência; a autorização vale apenas para os conteúdos, destinatário, ordem e intervalos apresentados.
- Uma resposta, descadastro, bloqueio ou resultado incerto pausa imediatamente os próximos toques.
- Troca de canal ou alteração de conteúdo nunca acontece silenciosamente; volta para revisão humana.
- A interface mostra a linha do tempo comercial, não uma fila técnica ou agenda de workers.

### Pesquisa e inteligência ABM

- A pesquisa começa pela conta e pelo momento empresarial, antes de escolher o contato ou escrever a mensagem.
- Um post, vaga, notícia ou mudança de liderança é sinal para investigar, não prova automática de intenção.
- O dossiê deve explicar fatos da empresa, momento, hipótese de oportunidade, hipótese alternativa, aderência ao ICP, comitê provável, riscos e próxima ação.
- A conexão com a Thunder Mustard usa apenas oferta, método e provas presentes nas fontes canônicas do `brain/thunder`.
- Cases demonstram capacidade e analogia; nunca prometem que o possível cliente repetirá o resultado.
- Pesquisa pode concluir que a conta deve ser acompanhada, pesquisada mais ou não priorizada. “Abordar” não é saída obrigatória.
- Toda evidência mantém URL, área de origem, classificação, data de publicação quando disponível e data de coleta (`observed_at`). A cobertura separa URLs planejadas, tentadas, descobertas e usadas como evidência.
- Fato público sem data de publicação é `undated`; publicação é recente por até 90 dias. A coleta precisa ter ocorrido nos últimos 14 dias para liberar abordagem.
- Expansão, vaga, anúncio ou mudança de liderança são indicadores, não intenção explícita de compra. O dossiê registra contrassinais com severidade/disposição proporcionais e cobertura de fontes com resultado, ausência pesquisada, tentativa incompleta, inacessibilidade ou falta de endpoint.
- Cada fato é ligado a uma implicação, uma explicação alternativa, uma hipótese de valor limitada e uma pergunta refutadora. Conteúdo externo é dado, nunca instrução.
- “Abordar” exige evidência atual ligada ao domínio oficial, ao menos duas URLs públicas distintas e um sinal recente datado. Intenção explícita, identificação/tempo no cargo e acesso às bibliotecas de anúncios não são requisitos universais; quando faltarem, a pesquisa registra lacunas nomeadas, gravidade, pergunta refutadora e se bloqueiam a hipótese. Resultado não encontrado só conta como busca concluída quando houve consulta verificável.
- Copy com pesquisa incompleta pode existir apenas como rascunho humano com aviso dos faltantes. O cliente não declara suficiência e Hermes não pode despachar esse rascunho.
- Fontes públicas e dados já disponíveis vêm primeiro; pesquisa paga ou reveal exige autorização específica.

### Legado

- Thunder Cockpit e Thunder GTM não serão recuperados como fundação.
- O código legado não será importado em massa.
- Uma função ou adaptador antigo só pode ser considerado quando houver necessidade concreta e uma auditoria mostrar que reaproveitá-lo é menor e mais seguro do que reescrever.
- O novo produto não depende de serviços, bancos, releases ou filas do legado.

### Entrega

- Existe um PRD canônico e uma sequência de implementação, não vários planos concorrentes.
- A implementação será incremental para permitir testes e rollback, mas todas as fases pertencem ao mesmo produto e ao mesmo contrato.
- “Pronto” significa jornada real verificada no runtime, não somente código, teste local ou tela publicada.

### Agent's Discretion

- Organização interna dos módulos e nomes técnicos que não apareçam ao usuário.
- Escolha entre evoluir uma fonte atual ou criar armazenamento aditivo, depois de mapear leitores, escritores e integridade.
- Detalhes de layout que preservem a simplicidade, acessibilidade e identidade já aprovadas para o Painel.
- Seleção de bibliotecas somente quando reduzirem complexidade total e tiverem contrato de manutenção claro.

### Declined / Undiscussed Gray Areas → Assumptions

- A V1 é interna, single-workspace e orientada ao Ygor; multiusuário e SaaS ficam fora.
- LinkedIn e e-mail formam a cadência da V1; HubSpot recebe o espelho e WhatsApp permanece manual.
- Histórico operacional é preservado na V1; exclusão e retenção regulatória detalhada ficam para uma decisão específica.
- Retry de efeito externo é manual e limitado a `provider_rejected`, `email_rejected`, `hubspot_rejected` e `channel_not_configured`; resultado incerto exige reconciliação.

## Specific References

- O Painel novo já promovido em `painel.r3xdigital.tech` é a interface inicial e não um protótipo descartável.
- A regra visual e operacional é permitir que uma pessoa leiga confira o projeto sem enfrentar logs, arquitetura ou atraso.
- A experiência existente `Hoje`, `Conversas`, `Para aprovar` e `Contas` é preservada e evoluída, não substituída por outra central.
- As fontes locais atuais da Thunder são `thunder/empresa.md`, `thunder/icp.md`, `thunder/site-oferta-e-cases.md`, `thunder/provas.md` e `thunder/cadencia.md`; divergências entre elas precisam ser resolvidas no design antes de virarem regra executável.

## Deferred Ideas

- SaaS ou operação para outras empresas.
- Autopilot e execução em lote.
- Aplicativo móvel nativo.
- Desativação ou limpeza dos sistemas legados.
- WhatsApp automatizado e canais adicionais.
