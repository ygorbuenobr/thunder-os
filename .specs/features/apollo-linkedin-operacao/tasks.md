# Plano de execução: Apollo + LinkedIn e respostas

## Test Coverage Matrix

| Critérios | Prova mínima prevista | Situação |
| --- | --- | --- |
| AO-01–03, AO-06, AO-12 | leitor com páginas vazias/parciais, tarefa alterada/removida, vínculos e chave desligada | pendente |
| AO-04–05, AO-09 | SQLite oficial temporário, dois processos, disputa e falha ambígua | pendente |
| AO-07–08, AO-10 | transporte HTTP fake para mensagem/convite, recibo persistido, conclusão e reconciliação só Apollo | pendente |
| AO-11 | limite por conta, janela, calendário, intervalo e sem rajada | pendente |
| AO-13–15, AO-20–22 | fontes de resposta, histórico/coorte, CRM não configurado e alerta/contexto | pendente |
| AO-16–19 | pausa local e releitura externa, indisponibilidade, resposta em voo e zero repetição | pendente |
| AO-21 | permissão gestor/admin, justificativa auditada e sem retomada automática | pendente |

## Gate Check Commands

- `python3 -m unittest discover -s _ferramentas -p 'test_thunder_os_apollo_linkedin*.py'` com `PYTHONPATH` e temporários isolados.
- Rodar testes focados dos adaptadores alterados, concorrência e painel; depois a suíte completa, comparando por identificador com a base limpa da mesma plataforma.
- `python3 scripts/check_source_boundary.py` e `git diff --check`; revisão independente sobre o diff e os critérios. Todos os testes offline, credenciais falsas, nenhum envio.
- A prova da integração real de pausa depende de recurso e escopo documentados; qualquer teste em produção e envio só dentro das autorizações efetivas. Publicação de código não comprova implantação ou jornada.

## Execution Plan

Estado local: descoberta e execução de mensagem por campanha, reserva idempotente,
worker de ciclo único, vínculo de identidade, webhook, reconciliação de eventos
antecipados e parada local possuem implementação e testes offline, desligados por
padrão. O monitor Apollo cobre somente contatos com vínculo e reserva comprovados;
não comprova a parada externa para seu sinal agregado. A ponte de contenção no
Apollo está desligada e sua confirmação externa não foi provada. Convites seguem
bloqueados por falta de prova de recibo conforme o contrato vigente. A identidade
comercial, o histórico entre canais, a cobertura HubSpot configurada, a parada por
empresa prospectada e a retomada segura de ações bloqueadas antes do POST ainda
não estão completos. Nenhuma peça está homologada nesta rodada em produção.
Um leitor de coorte Apollo independente da tarefa LinkedIn foi acrescentado e
testado offline; continua isolado do monitor, não prova histórico completo e
ainda depende de campanha com binding Apollo + LinkedIn aprovado. As consultas
HubSpot existentes não comprovam tenant/portal/credencial nem classificação de
resposta; campanhas que exigem CRM permanecem bloqueadas com cobertura unknown.

```text
T1 -> T2 -> T3 -> T4 -> T5 -> T6 -> T7
```

### Phase 1 — execução e segurança

Implementar e verificar T1, T2 e T3, em ordem. Mensagem T4d existente é ponto de partida, sem reimplementar sua reserva ou inventar uma segunda autoridade.

### Phase 2 — resposta, pausa e operação

Implementar e verificar T4, T5 e T6; fechar com T7. As tarefas de documentação e código são incrementais e não implicam ativação de runner ou envio.

## Task Breakdown

### T1: Reconciliar contratos e provas existentes

Where: `apollo_linkedin_live_executor.py`, `apollo_linkedin_reader.py`, `db.py`, specs existentes.
Depends on: none
Tests: testes de regressão existentes de mensagem, reserva e fonte; registrar lacunas específicas.
Gate: sem duplicar autoridade e sem tocar em fixture protegida.

### T2: Convite e recibo de envio

Where: `apollo_linkedin_message.py`, `connectsafely_client.py`, adaptador de convite ou módulo existente e testes.
Depends on: T1
Tests: convite confirmado, paginação incompleta, convite prévio, quota semanal, exceção e retry incerto com HTTP fake.
Gate: recibo durável da conta/destinatário antes da conclusão Apollo; nenhuma rede real nos testes.

### T3: Runner limitado, trava e conta

Where: `apollo_linkedin_live_executor.py`, leitor e configuração de serviço.
Depends on: T2
Tests: lista vazia/parcial, duas leituras e dois processos, locks, releitura JIT, quota/ritmo, chave desligada e múltiplas contas sem troca silenciosa.
Gate: agendamento e envio reais desativados até autorização; erros e cobertura parcial visíveis.

### T4: Resposta e parada persistente

Where: `apollo_linkedin_reply_coverage.py`, `apollo_linkedin_runtime_safety.py`, `reply_control.py` e testes.
Depends on: T3
Tests: resposta por fonte, ausência e indisponibilidade, identificação de empresa prospectada, replay, sem retomada automática e CRM não configurado.
Gate: parada local durável ocorre antes de próxima ação, isolada por empresa.

Para HubSpot, implementar primeiro parametrização segura de portal/owner e vínculo
da credencial ao portal por tenant, reaproveitando os leitores de elegibilidade e
histórico. Validar contato e empresa únicos, hash e releitura dentro do escopo da
campanha; acrescentar marco temporal confiável do último toque antes de interpretar
inbound. Provar com HTTP falso: portal/credencial trocados entre tenants, contato
ou associação ambígua, portal desconhecido, vínculo alterado, 429/cobertura parcial,
resposta anterior e posterior ao marco, classificação ausente e retomada de leitura.
Cobertura desconhecida bloqueia; sem CRM configurado, indicar `not_configured`.

### T5: Pausa Apollo e resposta em voo

Where: `reply_control.py`, adaptador Apollo e `apollo_linkedin_live_executor.py`.
Depends on: T4
Tests: pausa local → externa comprovada, falha e releitura, resposta durante POST com recibo e tarefa não concluída, reconciliação sem novo envio.
Gate: provar capacidade e escopo na documentação oficial; caso ausentes, manter pendência visível e não alegar pausa externa.

### T6: Atenção, contexto e operador

Where: `apollo_linkedin_attention.py`, `painel_servidor.py`, `painel_modelo.html` e testes.
Depends on: T5
Tests: urgência com histórico e cobertura, indicação de pausa local/externa, permissão e classificação, coorte versus histórico, estados vazios.
Gate: painel nunca rotula fila como recibo nem pausa externa não comprovada como concluída.

### T7: Validação e revisão independente

Where: testes abrangendo fluxo, `scripts/check_source_boundary.py`, artefato de verificação.
Depends on: T6
Tests: integração fake ponta a ponta, testes focados e suíte completa comparada à base por identificador, revisão independente de diff e evidências.
Gate: relatório por critério, limites de produção, qualquer bloqueio e instrução exata de próxima ação; sem envio não autorizado.
