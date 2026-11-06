# T4b — composição Apollo + LinkedIn com portões

## Critérios verificáveis

1. Execução desligada por padrão e sem timer; CLI exige tarefa e ID de autorização, com dry-run como padrão; fixture protegida é sempre recusada.
2. Aprovação ativa e não vencida usa mecanismo existente e vincula hash da configuração completa, política e versão; registro isolado não é aprovação; preservar A7 para matrículas já iniciadas.
3. Ator de escrita vem de proxy autenticado; arquivo de papéis root, configurável e validado falha fechado; somente gestor/admin podem resolver incerteza justificada ou criar canário.
4. Resolução e evento auditado são atômicos; resolver não envia nem implica nova tentativa automaticamente.
5. Autorização canário contém empresa, tarefa, conta, destinatário, hash e validade máxima de 30 minutos; consumir atomicamente junto da reserva; falha em qualquer lado reverte ambos.
6. Leitores de respostas ConnectSafely e Apollo e, quando configurado, HubSpot fornecem estado e observação atualizada em até 15 minutos; reply pausa; unknown ou cobertura parcial bloqueia.
7. Compartilhar lock por conta com todos os caminhos de envio ou bloquear T4 enquanto qualquer caminho sem lock estiver ativo ou seu estado for desconhecido. Reler pausa e chave dentro do lock antes do POST; preservar serialização contra atualização concorrente.
8. Compor cliente ConnectSafely e conclusão Apollo apenas depois de todos os portões, reserva e validação. Confirmar recibo com ID ou lista completa; convite prévio e listas incompletas não geram confirmação. Concluir Apollo somente após recibo durável, nunca repetir LinkedIn na reconciliação.
9. Preparador lista requisitos sem executar e fornece comando com tarefa e autorização, sem valorizar como pronto um portão desconhecido.
10. Exercitar sucesso e falhas com transportes HTTP falsos sem rede; comparar suíte e executar source-boundary/diff-check. Código, testes, integração real e deploy são evidências distintas.

## Restrições

Sem execução real, alterações na VPS, timer, campanhas ou fixture protegida. A aprovação para o primeiro envio real permanece uma frase específica de Ygor; esta documentação não autoriza envio. A verificação do lock e da pausa deve cobrir caminhos que não cooperam, ou bloquear a ação; não substituir prova por boolean fornecido pelo chamador.

## Estado da composição local

Os componentes de aprovação, autorização de canário, reserva e cobertura têm testes isolados, mas não constituem uma jornada de execução integrada. A revisão de segurança não aprovou o envio real: a emissão HTTP do canário retorna 503, a identidade do aprovador e o vínculo ator–empresa ainda não estão ligados ao fluxo de campanha, a cobertura não está conectada ao executor e o POST de conclusão no Apollo permanece desabilitado. A versão antiga também depende de prova durável de matrícula para atender A7.

O lock por conta ainda não protege todos os caminhos de envio e a pausa não está serializada com o POST. Antes de habilitar a composição, resolver esses contratos, provar concorrência e recibo com transportes falsos e repetir a revisão independente. Enquanto isso o CLI falha fechado; o preparador informa verificações desconhecidas como desconhecidas. A fixture permanece protegida.

O índice Git contém alterações parciais de T4 e T4b preparadas em sessão paralela; deve ser revisto e separado pelo responsável pelo commit. Nenhum arquivo do índice foi alterado por esta rodada de validação.
