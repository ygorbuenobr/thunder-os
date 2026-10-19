# Descarte local de cartão pendente

Sources: solicitação de Ygor nesta conversa; AGENTS.md; main b9b69f3 (preservar Agentes).
Profile: light; validação de navegador adicional solicitada pelo usuário.

## Out of scope
Publicação, providers, CRM, permissões, serviços e dados de produção.

## Landing
Reusar Banco, lock do painel, autenticação de nonce e leitura do servidor.
| Porta | Forma literal | Alternativa rejeitada |
|---|---|---|
| Endpoint específico | POST /api/os/v1/items/{id}/discard, corpo vazio, somente Ygor | Liberar PATCH genérico permitiria outros efeitos |
| Histórico atômico | _descarte com ator/data dentro do mesmo JSON SQLite; status descartado | Log em arquivo separado não acompanha a transação |
| Concorrência | lock do painel + BEGIN IMMEDIATE; terminal descartado recusa aprovação posterior | Somente checar status antes do lock deixa corrida |
| Leitura confirmada | GET autenticado /api/os/v1/items depois do POST; atualizar UI somente após achar descartado | Remoção otimista oculta falha de leitura |

## Checks
### S1 — Banco e HTTP · servidor e testes · ~30k
C1 — Pendente sem referência vira descartado sem provider. Proof: test_pending_discard_readback_and_count
C2 — Encaminhado/incerto/reconciliação recusa 409 com motivo. Proof: test_unsafe_states_refused
C3 — Retry não duplica histórico. Proof: test_repeat_is_idempotent
C4 — Ausência de identidade/nonce e ator não humano recusados. Proof: test_authentication_required
C5 — Descarte e aprovação concorrentes não deixam descartado encaminhado. Proof: test_discard_serializes_with_forwarding
C6 — PATCH genérico permanece bloqueado. Proof: test_legacy_patch_stays_blocked
### S2 — Painel e regressão · HTML · ~40k
C7 — Readback falho não remove cartão nem confirma sucesso. Proof: node scripts/test_panel_discard.cjs (readback failure)
C8 — Readback confirmado atualiza lista e contagem. Proof: node scripts/test_panel_discard.cjs (readback success)
C9 — Botões legados bloqueados mostram motivo antes de clique. Proof: node scripts/test_panel_discard.cjs (blocked actions)
C10 — Agentes e descarte navegam no navegador local. Proof: roteiro browser em relatório de entrega, evidência DOM/screenshot.

## Swept
Validação C2; falhas C7; idempotência C3; autorização C4; concorrência C5; lifecycle C1; dependência externa fora do descarte; transições C1/C2/C5; observabilidade histórico C3. Autenticação proxy existente não substitui TLS/auth de implantação.

## Handoff
S1+S2 ~70k, único lote sob 150k. Revisão independente depois do commit final, intervalo b9b69f3..HEAD; sem publicação.

### Conciliação adicional
Fixtures ConnectSafely passam a cadastrar campanha sintética e carregar campaign_review nos dois caminhos antigos; nenhuma assertion removida/relaxada. Rejeitada a alternativa de liberar execução sem contexto para satisfazer fixture antiga. PostgreSQL16 de quarentena exige ambiente próprio ausente; reportar separadamente, não suprimir o teste.
