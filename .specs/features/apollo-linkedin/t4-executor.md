# T4 — Apollo + LinkedIn: executor desligado por padrão

## Contrato e critérios de aceite

1. A execução é desabilitada por padrão, exige comando explícito, pause global liberada e canário com autorização única válida e tarefa allowlisted; a fixture protegida é sempre recusada. Não criar timer.
2. Empresa, sequência Apollo, donos, conta e versão vêm de campanha **aprovada** e vinculada à prévia, não de variáveis legadas; configuração cadastrada, isoladamente, não é aprovação.
3. Sob lock exclusivo da conta, reservar no SQLite oficial `(company, task_id, action_type)` antes do efeito, persistindo hash do texto, versão e destinatário; migração apenas aditiva. Reserva duplicada/conflitante nunca repete envio.
4. Reler tarefa imediatamente antes do efeito; exigir aberta, identidade e texto/hash íntegros e conta disponível. Remoção, divergência ou leitura incompleta bloqueia.
5. Checar resposta humana e supressão com cobertura suficiente; humana ou cobertura desconhecida bloqueia e deve interromper passos seguintes conforme controle já existente.
6. Aplicar no máximo um toque por lead/dia, separação configurada de 40–90 segundos e menor limite entre política semanal e saldo ConnectSafely; quota não comprova ações externas ao fornecedor.
7. Executar convite pelo cliente existente só após todas as barreiras. Falha comprovada antes do envio é `rejected`; timeout/ambiguidade é `uncertain` sem reenvio automático.
8. Confirmar somente com recibo válido ou confirmação de convite em histórico completo; convite prévio impede atribuição e permanece `uncertain`. Persistir evidência durável antes de concluir Apollo.
9. Concluir Apollo apenas depois de recibo confirmado; reler o status. Falha ou incerteza na conclusão vira `apollo_reconcile_pending`; reconciliar apenas Apollo, nunca reenviar LinkedIn.
10. Atenção expõe ações incertas, reconciliação e reservas órfãs. Resolução exige papel de gestor/admin verificável e justificativa auditada; sem RBAC comprovado, bloquear. Resolver não envia.
11. Testes isolados com provedores falsos cobrem sucesso, rejeição, timeout, ausência de recibo, convite anterior, mudança/remoção, resposta, quota, ritmo, reconciliação, disputa por conta, consumo de canário, fixture e desligamento. Sem rede, credenciais ou serviço externo.

## Limites conhecidos antes da implementação

`CampaignRegistry.binding/configuration` não estabelece por si aprovação nem inclui todos os campos de origem exigidos; não inventar vínculo. RBAC gestor/admin, conclusão Apollo real e semântica de recibo ConnectSafely exigem composição e validação explícitas. Uma dependência ainda não comprovada mantém o caminho bloqueado, nunca substituída por confirmação simulada. Prova com canário real exige autorização específica para envio e readback de ambos os provedores.

## Matriz de evidência local

| Critério | Evidência isolada | Limite |
| --- | --- | --- |
| 1 | `test_simulation_only_and_default_dry_run`, `test_cli_defaults_dry_run_and_never_enables_live`, `test_disabled_fixture_and_unapproved` | Canário real e composição segura não implementados. |
| 2 | `test_apollo_linkedin_metadata_is_versioned_but_not_approval`, `test_apollo_linkedin_metadata_rejects_unbound_or_invalid_routes` | Vínculos existem; aprovação de versão Apollo ainda não é fonte utilizável para execução. |
| 3 | `test_additive_migration_preserves_old_reservation`, `test_t4_independent_processes_share_one_reservation`, `test_account_lock_is_shared_across_processes` | Trava só é comum aos caminhos que a adotam; outros envios não comprovados. |
| 4 | `test_text_change_and_removed`, `test_protected_fixture_and_invalid_id_never_call_provider` | Provedor real não executado nos testes. |
| 5–6 | `test_reply_quota_pacing_and_unknown_block`, `test_human_reply_requires_persistent_pause`, `test_invalid_nonfinite_pacing_blocks` | Portas simuladas; cobertura cross-channel e pausa Apollo reais não integradas. |
| 7–8 | `test_rejected_before_dispatch`, `test_timeout_unknown_and_no_replay`, `test_missing_receipt_never_completes`, `test_prior_invitation_uncertain_before_send`, `test_success_durable_receipt` | Fluxos com fakes, não provam recibo ConnectSafely real. |
| 9 | `test_confirm_and_reconcile_after_apollo_failure_without_resending`, `test_two_processes_cannot_make_completion_attempt`, `test_reconciliation_is_read_only_against_apollo` | Composição coordenador/adaptador ainda incompatível; nunca habilitar. |
| 10 | `test_uncertain_resolution_fails_closed_without_verified_roles`, `test_attention_keeps_reconcile_pending_and_scopes_company` | Resolução humana retorna 503 até existir RBAC e transição auditada. |
| 11 | `test_lock_contenders_and_canary_consumed`, `test_t4_two_processes_cannot_claim_same_apollo_completion` e testes listados acima | Canary é falso; testes não demonstram consumo real nem operação externa. |
