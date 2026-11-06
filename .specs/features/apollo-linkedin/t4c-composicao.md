# T4c — mensagem de canário Apollo + ConnectSafely

Fonte: solicitação de 06/11/2026. Execução desligada por padrão, sem timer, rede nos testes ou envio real; a fixture `6aea584a2b8c4b0020f93935` permanece bloqueada.

| Contrato | Prova local exigida |
| --- | --- |
| Ator autenticado e papel de gestor/admin; canário vinculado a empresa, tarefa, conta, identificador, URN, hash e versão aprovada | Testes HTTP de atenção, papéis e armazenamento; divergência não consome a autorização |
| Reserva e consumo de canário atômicos, concorrência e ausência de reenvio incerto | Testes de armazenamento com dois processos e falhas de transação |
| Mensagem exige relação CONNECTED; recibo por ID ou releitura completa, exata e posterior à reserva | Testes do adaptador de mensagens com HTTP falso, incluindo recibo ausente |
| Resposta ou cobertura desconhecida bloqueia; pausa e chave são verificadas na fronteira de envio | Testes de integração com fakes, inclusive corrida com pausa |
| Todos os caminhos de envio da conta compartilham lock ou execução fica bloqueada | Testes de contenção entre processos e prova do estado de serviços concorrentes |
| Apollo só conclui após recibo durável; falha gera reconciliação exclusiva Apollo | Testes do adaptador de conclusão e integração sem rede |
| Preparação apenas lê e explicita cada evidência ausente; CLI só envia com todos os portões | Testes de CLI e preparação, com fixture bloqueada e execução desabilitada por padrão |

O comando é `PYTHONPATH=_ferramentas python3 -m thunder_os.apollo_linkedin_t4 --task <id> --authorization <id> --execute`. Ele não constitui autorização para enviar; a autorização específica de Ygor continua necessária. Estado em produção não pode ser inferido dos testes locais.

## Situação local

Há uma composição injetável exercitada com transportes falsos, mas **o CLI continua bloqueado**: a fábrica de runtime ainda não foi montada com credenciais e fontes oficiais comprovadas de último toque, quota e ritmo. A consulta à unit systemd não comprova sozinha a ausência de outros emissores, nem garante que serviços em execução usem o mesmo armazenamento e lock. O painel só emite um canário se a empresa, aprovação e leitores estiverem configurados e comprovados.

O envio manual e o nativo passaram a adquirir o mesmo lock no código local; acesso entre UIDs e o código carregado nos serviços não foram comprovados. A correção de paginação mantém `uncertain` quando a leitura é contraditória. A conclusão Apollo exige recibo durável; sua reconciliação de leitura pode verificar a situação mesmo com a chave desligada. Nenhuma dessas alterações prova operação em produção.
