# T4d — composição de mensagem do canário

Fonte: decisão de produto de 06/11/2026. A composição usa a campanha e o armazenamento oficiais, autorização de uso único e credenciais configuradas por instalação. O executor permanece desligado por padrão, sem timer; envio real exige autorização humana separada.

O emissor CLI de aprovação, executado por root com `--actor ygor` validado como admin, é aceito para o canário. A interface do painel será tratada depois. O canário vincula campanha aprovada, tarefa, conta, destinatário e hash; a reserva e o consumo são atômicos. A tarefa protegida continua sempre bloqueada.

O comando de execução desta fase usa `python3 -m thunder_os.apollo_linkedin_live_executor`; a entrada legada `apollo_linkedin_t4` permanece de simulação e não é uma alternativa operacional.

São aceitos para este canário: verificar a unit Hermes inativa e usar o lock por conta com o envio manual; manter uma transação SQLite durante uma única chamada de envio; e o processo autorizado do painel conservar a credencial de escrita. Isso não comprova os controles em outros hosts ou instalações.

Cobertura de respostas exige Apollo por ID do contato e ConnectSafely por perfil, mesmo sem e-mail; HubSpot não configurado não vira desconhecido. Pausa, chave, resposta, quota, ritmo, relação e identidade são verificados antes do envio. Falta de recibo comprovado gera resultado incerto e não autoriza repetição. A conclusão Apollo acontece só após persistência do recibo e a reconciliação não repete o envio LinkedIn.

Provas: testes offline com transportes falsos do caminho aprovação → autorização → reserva → mensagem → recibo → conclusão; cenários de e-mail ausente, serviço ativo, quota zerada, ritmo inferior ao limite, resposta prévia e chave desligada. Comparar suíte completa com a base, verificar fronteira do repositório e diff e pedir revisão independente. Nenhum identificador ou dado de contato real pertence aos exemplos ou testes deste repositório público.
