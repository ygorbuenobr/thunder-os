# Avaliacao local de trajetoria: backlog Thunder OS

Status: preparacao offline; **release gate OFF**. Nenhuma ativacao de gate,
provider, producao ou CI foi implementada. T4/T5/T6 live continuam bloqueadas
conforme o escopo recebido; nao foram verificadas externamente nesta subtarefa.
O plano mestre condiciona o gate de release a T2-T7 em producao. Testes locais
nao satisfazem esse requisito.

## Fonte e ambiente

- Verificado em 21/10/2026: host `Mac`, Darwin, usuario `ygorbueno`, Python 3.14.3.
- Checkout: `/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`.
- Branch: `codex/cadencias-readonly`; HEAD base:
  `c2ec4101da7ca7f8e8b1318e16774b6b7919c9e3`.
- O worktree ja continha alteracoes paralelas da T7; elas nao foram editadas.
- Lidos integralmente: `plano-mestre-thunder-os.md` e os sete arquivos de
  `cadencia-multicanal/eval/` no Brain: README, contrato, rubrica, cobertura,
  30 casos JSONL, validador e seus testes. Nenhum arquivo do Brain foi alterado.
- Esta fatia continua o trabalho existente; nao cria outro plano ou auditoria.

## Implementacao e isolamento

Novo teste: `_ferramentas/test_thunder_os_eval_trajectory.py`.

Classes reais exercitadas: `ThunderService`, `WorkerRuntime`, `Repository`,
`TouchLedger`, `EmailAdapter` e `AgentRunner`. A pesquisa sintetica reutiliza
`base_artifact()` de `test_thunder_os_research` e passa por `submit_research`,
catalogo validado, `create_cadence` e aprovacao reais. Nenhum gate de autorizacao,
pesquisa, deduplicacao ou worker foi substituido por um mock permissivo.

Somente o transporte de e-mail e a resposta textual do agente sao doubles.
`FakeMailbox` usa IDs e resultados fixos por cenario e preserva uma mensagem
possivelmente enviada antes de lancar timeout. Os bancos de workflow e toques
sao arquivos distintos em `TemporaryDirectory`; nao ha banco, porta, fila ou
credencial compartilhada. O schema SQLite de toques e uma fixture minima para
as consultas reais, nao uma migracao nem prova de compatibilidade PostgreSQL.
Sockets e o acesso padrao `toques._banco` sao bloqueados durante cada teste.

A dependencia `toques.py` vem da copia local preexistente
`.scratch/dependencies/toques.py`, conferida contra `runtime/dependencies.json`:
SHA256 `84eb0c7a86fe8fbe72c11427e747895b30d6acc66135eb36e4a8d3aa28e1e875`.
O teste falha se a identidade divergir; nao instala nem copia dependencias.
Outro checkout precisa disponibilizar essa dependencia aprovada no PYTHONPATH.

## Reproducao e captura

Executar na raiz do checkout:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:.scratch/dependencies \
  python3 -W error::ResourceWarning -m unittest test_thunder_os_eval_trajectory -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:.scratch/dependencies \
  python3 -m test_thunder_os_eval_trajectory --trace -q
```

O segundo comando emite JSON em stdout; a saida do unittest fica em stderr.
O exit code acompanha os testes. O JSON contem `release_gate: OFF`, nove
capturas de trajetoria e seis probes diagnosticos de copy. Nenhum arquivo de
evidencia e criado fora dos dois arquivos atribuidos; os bancos e healthchecks
temporarios sao encerrados com o teste. IDs de provider e resultados sao
deterministicos; timestamps e hashes que incluem tempo variam por execucao.

Cada captura contem o payload `command` lido do repositorio, os comandos
recebidos pelo fake, `receipt`, `readback`, `finalstate` relido por outra
instancia de `Repository`, status do ledger e estados das cadencias.

**Readback e um observador fake do harness.** O worker real nao chama esse
readback: persiste `sent` a partir do recibo. O teste compara mensagem observada
e comando, mas nao promove o estado para `verificado`. Sem recibo, a captura
marca `requested: false`; nao inventa uma confirmacao. A aceitacao de uma
resposta natural do agente ou do resultado por um usuario nao foi avaliada.

## Alcance dos testes

| Cenario | Enforcement real observado | Relacao com dataset |
| --- | --- | --- |
| Aprovacao exata | Texto/alvo/hash corretos; fila sem efeito; worker envia uma vez; recibo e ledger persistidos | 005/008, parcial: readback e observador |
| Ausente ou generica | Confirmacao rejeitada, sem aprovacao ativa, fila ou provider | 004 |
| Outro alvo, texto, canal ou campanha | Confirmacao com escopo diferente rejeitada | 016/017/028 |
| Payload alterado depois da fila | Worker termina `failed/campaign_confirmation_scope_changed`, sem reserva/envio | 017 |
| Timeout depois de possivel efeito | `uncertain/provider_timeout`; reenqueue, novo polling e novo worker nao repetem; retry direto rejeitado | 010/021/023, parcial: sem reconciliacao real |
| HTTP 200 sem ID de recibo | `uncertain`, recibo ausente; nao afirma `sent` | 030, parcial: sem resposta textual do agente |
| Duplicidade | Mesmo comando produz um efeito; outro lote com mesmo toque exige reconciliacao | 006/022, parcial: prevencao, nao incidente de dupla execucao consumada |
| Resposta depois da fila | E1 fake confirmado; resposta correlacionada pausa cadencia; E2 enfileirado falha; ledger tambem bloqueia com `has_reply` | 029, parcial: parada interna, sem ferramentas externas |
| E2 sem E1 confirmado | `dispatch_step` recusa falta de predecessor confirmado | Apenas dependencia operacional generica |
| Prova nao aprovada ou referencia omitida | `AgentRunner` retorna `needs_human/invalid_agent_output` | Enforcement de referencias, nao de qualidade da copy |
| Dependencia | Hash real de `toques.py` igual ao manifesto | Reprodutibilidade local |

Sao 11 testes focados, nao a execucao automatica dos 30 casos do Brain. Os IDs
na captura indicam correspondencia tematica, nao aceite integral desses casos.
Nao ha pontuacao agregada, classificacao de release aprovada ou cobertura de
identidade empresarial completa, expiracao, pausa externa, quotas, fuso,
contradicao de recibo, crash concorrente ou canais reais nesta fatia.

## Seis criterios criticos de copy Mexico

Os probes chamam o `AgentRunner` real com JSON deterministico. Nao ha regex,
validador de copy ou resultado esperado inventado no harness para aprovar
esses criterios. Eles sao diagnosticos sem assertions de qualidade; seus
resultados nao contam como testes verdes de aderencia a skill.

| Criterio | Observacao | Lacuna |
| --- | --- | --- |
| E2 sem E1 real | Probe sem E1 aceito pelo parser; executor exige predecessor `sent` | Nao prova que a copy partiu do texto real do E1 nem que houve readback externo do E1 |
| E1 generico | Texto generico com `angle_id` e evidencia vazia aceito | Nao foi encontrado enforcement de especificidade da conta nessa fronteira |
| Sem prova aprovada | Sem `proof_ids` declarados, copy sem prova aceita; referencia nao aprovada ou omitida quando exigida e rejeitada | Catalogo/referencias nao obrigam prova no texto nem verificam sua fidelidade |
| CTA errado | CTA sintetico fora do criterio solicitado aceito | Sem validacao do CTA autorizado da campanha nessa fronteira |
| Abertura "Vi que" | Abertura aceita no probe | Sem enforcement dessa restricao de linguagem nessa fronteira |
| E2 repete introducao | E1 e E2 iguais no contexto do probe aceitos | Sem comparacao semantica com o E1 real nessa fronteira |

Os seis probes retornaram `succeeded`. Isso revela aceitacao estrutural,
nao qualidade ou autorizacao de envio. As conclusoes se limitam ao parser real
`AgentRunner._parse` inspecionado/exercitado: nao demonstram comportamento de
um LLM real, da skill comercial carregada ou do executor externo do Mexico.
O runner `scripts/mexico_e1_runner.cjs` tem fluxo especifico E1; nao foi usado
como prova de geracao de E2 nem executado nesta subtarefa.

## Validacao e handoff

- Baseline: 31 testes existentes de `test_thunder_os_campaign_confirmation` passaram.
- Nova suite: 11 testes passaram com `ResourceWarning` promovido a erro.
- Captura `--trace`: nove trajetorias e seis probes; gate OFF.
- Source boundary permanece sob ownership do principal. Os dois novos caminhos
  precisam de classificacao na allowlist antes de commit/build, se ainda nao
  estiverem classificados; esta subtarefa nao altera o controle.
- Revisao independente, commit separado, build e qualquer futura ligacao do gate
  ficam com o principal e com as autorizacoes aplicaveis. Sem commit, push,
  deploy, rede, provider real ou alteracao dos arquivos da T7.
