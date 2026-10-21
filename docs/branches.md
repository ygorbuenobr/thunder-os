# Thunder OS: branches e worktrees

Inspecao em 21/10/2026. Inventario documental, nao mecanismo de autorizacao.
Plano oficial: `~/thunder-gtm/projetos/thunder-os/plano-mestre-thunder-os.md`.
Plano v2 confirmado no Mac: 12.424 bytes, commit Brain `1827e53`, com
Tarefa 0 (reconciliacao) e Tarefa 1 (plataformas externas). Plano somente lido.
Tarefa 0 preparada para revisao, nao entregue: o aceite exige igualdade exata
entre o SHA do GitHub main e o da producao do painel, ainda pendente.
Nao iniciar Tarefa 1 antes do aceite da 0. Nenhuma ativacao autorizada agora.

## Autoridade e publicacao pendente

- GitHub: `https://github.com/ygorbuenobr/thunder-os`, main remoto registrado
  em `95ac71a851cf1ef8fad84eb03362d60821f0949c`, tambem em origin/main local.
  Esta revisao documental nao consultou o GitHub nem a VPS.
- Painel VPS `srv1433640`, conforme evidencia anterior: `/opt/thunder-os/current` aponta para
  `/opt/thunder-os/releases/b14a3aa55d4fec1a4f16762aacd2fe85f32ed174`.
  A verificacao anterior de saude registrou esse SHA e PID2789921.
  Publicacao registrada; runtime nao revalidado nesta revisao documental.
- Agente OS: `/opt/thunder-os/agent-current` permanece na linha `d562788`;
  nao confundir com o painel, nem alterar seu runtime nesta reconciliacao.
- Os commits `bba3edc` (briefing) e `b14a3aa` (cadencias) estao na producao
  do painel e ainda faltam no main remoto.
- A branch `codex/cadencias-readonly`, no worktree
  `/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`, e a unica fonte
  designada para a futura publicacao, pendente do alinhamento de main.
  Sua base `b14a3aa` corresponde a producao registrada; as alteracoes locais
  de inventario e classificador sao candidatas, ainda nao publicadas.
  Esta revisao edita somente `docs/branches.md`; o classificador tem outro dono.
- Antes de push: revalidar main remoto e producao; confirmar fast-forward
  de `95ac71a` e diff sem ConnectSafely. Aguardar novo OK do Ygor.
- O main local ainda aponta para `51244c2`, nao e a fonte de publicacao.
  Ajustar sua referencia somente apos aprovacao, preservando a branch WIP.
  Nao executar reset, stash, merge da linha WIP ou force-push.
- Um commit sucessor apenas documental tambem muda o SHA: publica-lo no main
  nao satisfaz o aceite enquanto o painel permanecer em `b14a3aa`.
  Apos OK do Ygor, a publicacao futura deve alinhar main e uma nova release
  imutavel do painel ao mesmo SHA escolhido, com rollback preparado e
  verificacao do SHA efetivo em producao. Commit ou push nao comprovam isso.
  Preparacao, publicacao e ativacao sao etapas distintas; nenhuma ativacao,
  alteracao de release publicada ou restart faz parte deste ajuste documental.

## Worktrees locais

Classificacao documental por base e alteracoes, pendente de revisao do dono.
Historico nao significa abandonado; nenhuma referencia foi arquivada ou apagada.

| Caminho absoluto | Branch | Base inspecionada | Classificacao | Estado / destino |
|---|---|---|---|---|
| `/Users/ygorbueno/thunder/thunder-os` | `codex/connectsafely-local-wip` | `51244c2` | Candidato local, pendente de revisao do dono | Cinco arquivos ConnectSafely nao commitados, sem backup remoto; preservacao na troca de branch registrada anteriormente. Nao publicar junto do painel. |
| `/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias` | `codex/cadencias-readonly` | `b14a3aa` | Base em producao registrada; ajuste candidato | Inventario e classificacao preparados para revisao. Unica fonte de publicacao designada, pendente do alinhamento de main. |
| `/Users/ygorbueno/Documents/Codex/2026-10-19/task-2/thunder-os-confirmation` | `codex/campaign-execution-confirmation` | `bba3edc` | Candidato, pendente de revisao do dono | Alteracoes locais em classificador, commercial_continuity/factual_note_writer e testes; nao incluir. A base publicada nao torna essas alteracoes producao. |
| `/Users/ygorbueno/thunder/.worktrees/thunder-os-account-pilot` | `codex/account-next-move-pilot` | `5041176` | Candidato, pendente de revisao do dono | Alteracoes locais em .gitignore, classificador, checks e scripts do piloto; nao incluir. |
| `/Users/ygorbueno/thunder/agenttrail-panel` | `codex/agenttrail-panel` | `b9b69f3` | Historico, pendente de revisao do dono | Limpo na inspecao anterior; nao e o painel ativo. Preservar. |

## Outras referencias

Referencias remotas abaixo sao as registradas localmente, sem nova consulta remota.

| Branch | SHA observado | Classificacao | Localizacao / observacao |
|---|---|---|---|
| `main` | local `51244c2`, remoto `95ac71a` | Historico divergente, pendente de alinhamento e revisao do dono | Nao corresponde ao SHA de producao registrado. Preservar referencias ate aprovacao; nao usar o checkout WIP para publicar. |
| `codex/connection-catalog-format` | `0a17ccd` | Historico, pendente de revisao do dono | Local e remoto, sem worktree associado. Preservar. |
| `codex/connection-copilot-go-live-audit` | `4487d10` | Historico, pendente de revisao do dono | Local e remoto, sem worktree associado. Preservar. |
| `codex/reconcile-thunder-os-production-20261007` | `5ad7ca2` | Historico, pendente de revisao do dono | Local e remoto, sem worktree associado. Preservar. |
| `codex/shared-preparation-homologation-20261009` | `4e0d02d` | Candidato, pendente de revisao do dono | Referencia remota, sem branch/worktree local listado. Preservar. |

## Trabalho ConnectSafely preservado

A branch WIP conserva tambem os commits locais `4f5e974` e `51244c2`, de
supervisao Mexico, que nao pertencem a linha publicada do painel. Nao se
afirma que esses commits ou as alteracoes seguintes foram homologados.

Arquivos nao commitados mantidos no checkout principal:

- `_ferramentas/thunder_os/connectsafely_application.py`
- `_ferramentas/thunder_os/connectsafely_client.py`
- `_ferramentas/thunder_os/connectsafely_console.py`
- `_ferramentas/thunder_os/connectsafely_ledger.py`
- `_ferramentas/test_thunder_os_connectsafely_dm_reconciliation.py`

Uma branch preserva commits, nao faz backup remoto de alteracoes nao commitadas.
Esses cinco arquivos sao WIP local nao commitado, sem backup remoto; este
ajuste documental nao os altera nem cria backup. Sua revisao, commit e
publicacao sao uma tarefa separada. Nenhuma limpeza de worktrees autorizada.
