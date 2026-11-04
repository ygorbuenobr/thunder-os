# Apollo + LinkedIn: simulated implementation tasks

## Test Coverage Matrix

Guidelines: `AGENTS.md`, `README.md`; Python standard `unittest`, isolated fakes and temporary state.

| Code Layer | Required Test Type | Coverage Expectation | Location Pattern | Run Command |
| --- | --- | --- | --- | --- |
| Simulated reader and executor | unit and isolated integration | AL-01 through AL-21, including separate OS processes contending for one store, account lock and replay | `_ferramentas/test_thunder_os_apollo_linkedin_simulation.py` | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest _ferramentas.test_thunder_os_apollo_linkedin_simulation` |

## Gate Check Commands

| Gate Level | When to Use | Command |
| --- | --- | --- |
| Quick | Focused simulation changes | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest _ferramentas.test_thunder_os_apollo_linkedin_simulation` |
| Boundary | Before commit | `python3 scripts/check_source_boundary.py` |

## Execution Plan

Complete T1, then T2. Both are confined to fake providers and focused validation.

## Task Breakdown

### T1: Simulated reader and executor

**What**: Implement the isolated task reader, eligibility, state and fake-only execution.
**Where**: `_ferramentas/thunder_os/apollo_linkedin_simulation.py`, `_ferramentas/test_thunder_os_apollo_linkedin_simulation.py`.
**Depends on**: none.
**Done when**: AL-01 through AL-21 have focused passing tests, including separate processes sharing one temporary official store, account-lock contention, missing/unavailable-store initialization failure and immediate task reread fail-closed; no live adapter or scheduler is wired.
**Tests**: unit and isolated integration.
**Gate**: Quick and Boundary.

### T2: Independent review

**What**: Verify diff and requirements against test assertions; report evidence and residual risk.
**Where**: `.specs/features/apollo-linkedin/validation.md` if the independent verifier writes a report.
**Depends on**: T1.
**Done when**: reviewer verifies the diff and focused test evidence.
**Tests**: independent review.
**Gate**: Quick and Boundary.

## Phase Execution Map

Phase 1: T1 → T2

### T3: Apollo LinkedIn sem envio (em andamento)

Objetivo: reservas no banco oficial, lock por conta, leitura de tarefas, roteamento explícito, dry-run com releitura e atenção no painel. Nenhum envio ou escrita no Apollo é autorizado nesta rodada.

| Superfície | Prova | Estado |
| --- | --- | --- |
| Reserva e lock oficiais | `test_thunder_os_apollo_linkedin_storage` | Banco oficial por caminho canônico; testes isolados, concorrência e banco incorreto |
| Roteamento explícito e conta indisponível | `test_thunder_os_apollo_linkedin_reader` | Associação explícita; sem troca de conta |
| Leitura paginada | `test_thunder_os_apollo_linkedin_reader` | POST exato tasks/search; cobertura desconhecida se incompleta |
| Executor sem envio e releitura | `test_thunder_os_apollo_linkedin_executor` | Dry-run sem dispatch, sem consumir reserva |
| Atenção no painel | `test_thunder_os_apollo_linkedin_attention_http`, `test_thunder_os_apollo_linkedin_executor` | Rota GET, reservas, locks e bloqueios; integração fake |
| Limite do repositório | `python3 scripts/check_source_boundary.py` | Verificar após mudanças |
| Revisão independente | diff completo e testes | Não aprovado: credencial não vinculada ao cliente; GET grava lock transitório |

### Retomada rodada 4 — revisão e commit pelo Claude

- Base: `main` em `24facb2`; branch `codex/apollo-linkedin-t3` rebaseada. Não enviar, ativar executor/timer, alterar ou concluir a fixture Apollo, nem adicionar contatos.
- Alterações locais: credencial com validação no uso; GET sem acesso ao cliente de envio; orçamento de atenção e páginas, configuração inválida visível, fixture protegida e reservas órfãs; lease com exclusão por processo no mesmo host e caminho oficial configurável.
- **Limite relevante:** o GET não possui ainda um leitor ConnectSafely com credencial comprovadamente somente leitura. A prévia falha fechada com `readonly_account_unconfigured`, e atenção não comprova o vínculo de credencial nem conta disponível. Não declarar o item 2 completo nem publicar esta regressão como jornada operacional; confirmar capacidade de credencial restrita e integrar porta GET independente antes de aprovação.
- **Contrato de solução:** `design.md`, seção “Read-only account verification: T3 boundary”: verificar primeiro escopo de chave no fornecedor; se indisponível, desenhar broker isolado somente após comprovar fronteira de processo/credencial. Conta lida e vínculo da credencial de execução são provas distintas. Testes falsos devem provar impossibilidade de chamada de envio, isolamento e falha fechada. Esta proposta não ativa serviço nem concede autorização para mensagens.
- **Plano de comprovação do isolamento:** (1) verificar documentação do fornecedor e anotar que ausência de escopo na documentação não prova inexistência; (2) testar contrato local falso com operação e conta permitidas, rejeição de POST/rota arbitrária/tenant errado e resposta sem segredos; (3) antes de construir integração real, confirmar identidade efetiva do painel, permissão da chave e desenho de broker com Unix socket, credenciais e identidade separados; (4) em staging Linux, provar rejeição por peer e tenant, incapacidade do painel de ler a chave ou alterar broker e degradação por timeout/queda; (5) provar separadamente binding do executor, executar revisão independente e preservar rollout/rollback e executor desligado. Não usar mocks como prova de isolamento do sistema operacional.
- **Matriz de verificação da fronteira:** RO-01/RO-02 tiveram prova de separação de processo/identidade no staging sintético descrito abaixo, ainda sem prova no runtime real; RO-03/RO-04 exigem teste de identidade, erro, timeout e cobertura; RO-05 exige teste offline de envelope, conta, operação e filtragem; RO-06 exige evidência separada do executor; RO-07 exige provedor falso sem rede e prova das permissões do SO no ambiente real; RO-08 exige revalidar os controles do executor sem disparar. Teste do contrato não libera o painel nem o executor.
- **Protótipo offline, 04/11:** `connectsafely_read_broker_contract.py` testa envelope fechado, tenant/conta, duas leituras fixas através de provedor falso, validação de identidade, filtragem e tempo observado; 8 testes passam. Um callable falso não demonstra método HTTP de transporte real, limite de rede/tempo, peer UID, segredo isolado ou frescor no consumidor. Revisão pontual de segurança não encontrou bloqueio no protótipo; revisão final do diff pelo Claude permanece pendente. Próximo: validar no fornecedor se existe escopo read-only; se não for comprovável, verificar identidade efetiva do painel e especificar broker isolado implementável antes de integrar. Não alterar runtime nem acessar a fixture.
- **Staging Linux com dados falsos:** em `srv1433640`, somente em diretório temporário da tarefa, sob `unshare -n` com apenas loopback, `scripts/test_connectsafely_read_broker_staging.py` executou processos com 3 UIDs, `SO_PEERCRED` e chave sintética; testou GET HTTP real das rotas de prévia e atenção através de socket Unix, com Apollo e ConnectSafely simulados; negou acesso do painel à chave, diretório e memória do broker, rejeitou escrita e tenant errado, rejeitou terceiro UID e manteve `execution_binding_unverified`. Sem rede externa, credenciais reais, ação em provedor ou mudança de serviço. Isso não prova provider real ou isolamento da produção. Identidade `root` do painel ativo, ACLs e resolver/composição legados precisam de solução e prova separada antes de ativar leitura real. O script encerra os processos de teste; não instala um serviço persistente.
- A execução do leitor de atenção devolve cobertura parcial ao esgotar orçamento; uma leitura externa bloqueada pode continuar em thread somente leitura até seu timeout, com no máximo uma consulta em voo por instância. Validar o comportamento no diff.
- Locks legados sem expiração falham fechados; o flock exige host único e sistema de arquivos local. A revisão deve verificar que o processo antigo não pode atuar depois da expiração enquanto outro obtém o lock.
- Próximo passo: comparar falhas por identificador contra a suíte do `main` no mesmo ambiente com dependência `toques.py` de hash fixado; entregar diffs e logs ao Claude para revisão independente e eventual commit. Nenhuma alteração foi implantada.
- Validação: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas/test_thunder_os_apollo_linkedin*.py test_thunder_os_connectsafely_panel_wiring.LazyCredentialPathTests` → 103 testes OK. `python3 scripts/check_source_boundary.py` → OK.
- Suíte comparativa: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Users/ygorbueno/thunder/thunder-os/.scratch/dependencies:_ferramentas python3 -m unittest discover -s _ferramentas -p 'test_*.py'`. Base `24facb2` extraída em `/tmp/thunder-t3-round4-main`: 2849 testes, 42 falhas, 25 erros, 9 pulados; branch no checkout canônico: 2895 testes, 42 falhas, 17 erros, 9 pulados. Comparação dos identificadores `FAIL`/`ERROR`: 0 novos e 8 ausentes na branch. Os caminhos de execução diferem (arquivo extraído vs checkout); os 8 ausentes não provam correção. Logs: `/tmp/thunder-t3-round4-main-deps.log` e `/tmp/thunder-t3-round4-branch-deps.log`.
