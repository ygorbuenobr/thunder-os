# Painel não root e broker de leitura

## Implantado em 06/11/2026 (srv1433640)

- Release ativa do painel: `93807c67d809f86d102f401881bd70129d07feb8`. Rollback de release: apontar `/opt/thunder-os/current` para `1e204b9…`, `6acf9a7…` ou `7d5b3d1…` e reiniciar.
- `painel-thunder` roda como `thunder-panel` (UID 992), grupos `thunder-panel-db` e `www-data`, `NoNewPrivs=1` e capabilities zeradas. Drop-in: `/etc/systemd/system/painel-thunder.service.d/zzzzz-non-root.conf` (conteúdo = `runtime/systemd/painel-thunder-non-root.conf.candidate` sem comentários).
- Banco: `painel.db` com dono `thunder-panel:thunder-panel-db`, modo `0660`, e diretório `2770`. O monitor root gravou depois da troca e os arquivos mantiveram o dono.
- ACLs: travessia `--x` para `thunder-panel` em `/root/.hermes` e no perfil; `rwX`, com ACL padrão, no brain; leitura no arquivo de evidência do canário ConnectSafely.
- Credenciais via `LoadCredential`, a partir das fontes reais; os caminhos legados são resolvidos por `credential_paths.resolve_legacy`.
- Checagem do nginx sem root: peer com UID 33 e pai igual ao master do pidfile de root, e master rodando como root (risco aceito pelo Ygor em 05/11).
- Backup e rollback completo: `/var/backups/thunder-panel-nonroot-20261106T082915/rollback.sh`.
- A primeira tentativa (`20261106T082452`) falhou ao subir, porque o `canary_readers` lia a credencial do HubSpot por caminho fixo, e voltou sozinha ao estado anterior. A falha foi corrigida em `93807c6`.
- Pendente: confirmar no navegador o login autenticado e a prévia do LinkedIn. A checagem do nginx não pode ser testada sem a senha do painel.

---

Estado em 05/11/2026: **desenho bloqueado para implantação**. A decisão atual aceita o envio manual do ConnectSafely pelo painel e o adaptador T3 no mesmo processo (`in_process_accepted`); portanto a unit separada do broker é opcional e os passos relacionados abaixo só se aplicam quando essa opção for escolhida. Nenhum comando deste documento foi executado na VPS nesta rodada. Os caminhos operacionais do host foram relatados pelo Claude e precisam de nova verificação; o checkout e as units de exemplo não provam a configuração instalada. O painel tem capacidade de envio e escrita em CRM: mudar sua identidade pode interromper funções existentes. Não habilitar executor ou timer de LinkedIn.

## Inventário por famílias de acesso

Contagem limitada ao levantamento estático do repositório: 13 famílias de caminhos e pelo menos 7 categorias de credenciais. Há caminhos parametrizados, subprocessos, drop-ins e configurações externas; **não é um inventário exaustivo dos arquivos efetivamente acessados**. `R` = leitura, `W` = escrita, condicional = depende da configuração.

| Família | Caminhos ou variáveis | Acesso / consumidor |
|---|---|---|
| 1. Release | `/opt/thunder-os/current`, `_ferramentas/painel_modelo.html`, manifesto | R; código e renderização do painel. |
| 2. SQLite | `PAINEL_DB`, banco `painel.db` e `-wal`/`-shm`; relato: `/root/.hermes/profiles/ygor-personal/painel-dados/` | R/W; painel e outros processos. Banco existente fora do código/brain. |
| 3. Brain e insumos | `THUNDER_BRAIN_ROOT`, `contas/`, `thunder/`, `eu/`, `conhecimento/`, `fila-linkedin.md`, `projetos/thunder-os/` | R/W conforme rota, inclusive `contas/<slug>/campanha.md` e `_entrada/claude/painel/`. |
| 4. Logs/locks do brain | `_log/atividade.jsonl`, `_log/.painel.lock`, `_log/.unipile-*.lock` | W; preservar locks e concorrência com outros consumidores. |
| 5. Apollo | `/root/.config/thunder/credentials/apollo_api_key.txt`; `THUNDER_APOLLO_LI_*` equipe, conta, ator, campanha, tarefa e bindings | R, rota T3; caminho de credencial é fixo no código e não deve ser exposto aqui. |
| 6. Instantly e ingress | `/etc/thunder-os/instantly_api_key`, `instantly_reader_enabled`, `instantly_webhook_secret`, `apollo_reader_enabled`, `agenttrail-ingress.token` | R condicional; chaves/flags independentes. |
| 7. HubSpot | `THUNDER_OS_HUBSPOT_CREDENTIAL_FILE` ou `$CREDENTIALS_DIRECTORY/hubspot_access`; `THUNDER_OS_HUBSPOT_READ_ENABLED`, `THUNDER_OS_HUBSPOT_WRITE_ENABLED` | R condicional; escrita externa depende também de `THUNDER_EXTERNAL_EFFECTS_ENABLED`. |
| 8. SEQD | `THUNDER_OS_SEQD_CREDENTIAL_FILE` ou `$CREDENTIALS_DIRECTORY/seqd_bridge`; flags `THUNDER_OS_SEQD_READ_ENABLED`, `THUNDER_OS_SEQD_SEND_ENABLED` | R condicional; bridge HTTP loopback `127.0.0.1:8792`; envio depende de flags. |
| 9. ConnectSafely | `THUNDER_OS_CONNECTSAFELY_CONFIG`, hash e credencial referenciada; `THUNDER_APOLLO_LI_BROKER_SOCKET`, `_UID`, `_COMPANY`, `_ACCOUNT`, `_PUBLIC_ID` | Decisão 05/11: painel pode deter credencial de envio, com risco aceito; T3 usa adaptador somente leitura no mesmo processo, marcado `in_process_accepted`. Broker separado permanece opcional e suas credenciais não entram no painel por esse caminho. |
| 10. Ambiente e PostgreSQL | Relato: `/root/.hermes/profiles/ygor-personal/.env`; `THUNDER_DB_URL`, `THUNDER_EXTERNAL_EFFECTS_ENABLED`, `THUNDER_OS_COPILOT_MODE`, `THUNDER_OS_ACTORS`, `THUNDER_HERMES_CREDENTIAL` | EnvironmentFile lido pelo systemd; dados sensíveis nunca devem ser impressos. `toques.py` depende de Postgres em fluxos existentes. |
| 11. Painel/ingress | `PAINEL_PORTA` (padrão do código 8787; relato operacional 8790), `THUNDER_CONFIRMATION_SOCKET`, `THUNDER_CONFIRMATION_NGINX_UID`, `_EXECUTABLE`, `_PID_FILE` (padrão `/run/nginx.pid`), `THUNDER_OS_ORIGIN` | TCP loopback e socket Unix; diretório do socket deve pertencer ao UID efetivo e não ter escrita por grupo; socket é atribuído ao grupo nginx. Verificar grupo e conexão real do proxy. |
| 12. Agente/Hermes | `HERMES_CMD`, `/etc/thunder-os/hermes-ro-runtime-lock-v2.json`, `/var/lib/thunder-os-agent/`, perfil Hermes e binário configurado | R/W condicional e subprocessos; `PrivateTmp` e `ProtectHome` podem afetar IPC e acesso a arquivos. |
| 13. Mission A e políticas | `/root/.hermes/profiles/thunder-mustard/{config.yaml,cron/jobs.json,cron/executions.db,cron/output/}`, `/opt/thunder-os-staging/`; `THUNDER_OS_GMAIL_CADENCE_POLICY_FILE`, `_SHA256`, `THUNDER_OS_GMAIL_CRM_POLICY_FILE`, `_SHA256` | R/W condicional, inclusive subprocessos e backups; validar funções que realmente estiverem em uso. |

Categorias de credenciais verificadas no código: Apollo, Instantly API, webhook, AgentTrail ingress, HubSpot, SEQD e ConnectSafely; `.env`, Postgres e Gmail podem acrescentar outras. Não se leu existência, valor ou acesso de nenhum segredo nesta rodada.

### Escritores do SQLite

1. **Painel:** `Banco` e `Repository`, em `_ferramentas/painel_servidor.py`, abrem `PAINEL_DB` e gravam estados.
2. **Agente Thunder OS:** `continuous_consumer.py` instancia `Repository(args.db)` e grava estado; `runtime/systemd/thunder-os-agent-hermes-ro.conf` aponta `--db` para o caminho reportado do painel. É evidência de código e configuração de exemplo, não prova do processo atualmente ativo.
3. **Escritores condicionais:** `thunder-os-hermes-linkedin` e processos `connections-live`/`connections-backfill` usam o repositório configurado; só são escritores do mesmo banco se suas configurações instaladas apontarem para ele. `agenttrail` usa runtime próprio nas units do repo; não foi demonstrado que escreve nesse banco. Também não há prova local de escrita direta no mesmo banco por `hermes-gateway` ou `hermes-dashboard-thunder-mustard`. Confirmar cada processo e abrir arquivos no host antes de alterar permissões.

## Modelo de isolamento e permissões

- Painel: usuário de sistema `thunder-panel`, grupo primário homônimo, sem shell; broker: `thunder-li-reader`, UID diferente, sem shell, sem root e sem capabilities. Grupo `thunder-li-socket` permite ao painel conectar ao socket; o broker não integra o grupo de banco.
- `/run/thunder-li-reader`: dono `thunder-li-reader`, grupo `thunder-li-socket`, modo `0750`; socket criado pelo broker `0660`; a autenticação de peer deve validar o UID do painel. A credencial potencialmente capaz de envio do broker só pode ser lida pelo broker. A credencial de envio do ConnectSafely, e-mail ou HubSpot **não** fica disponível ao broker. Separação real exige prova dos acessos a arquivos, memória, processo e configuração.
- SQLite: diretório `root:thunder-panel-db` modo `2770`; banco `thunder-panel:thunder-panel-db` modo `0660`; WAL/SHM devem herdar esse dono/grupo e modo para os escritores autorizados. Painel com `UMask=0007`; o grupo de banco não deve incluir o broker. A hipótese de `fchown` dos auxiliares WAL/SHM pelo SQLite unix VFS ao dono do banco, inclusive quando criados por escritor root, **ainda não foi comprovada nesta rodada**: provar no ensaio Linux antes de qualquer migração; se falhar, interromper e revisar. Não presumir que `chgrp` em banco aberto ajuste arquivos futuros.
- Brain: ACL somente de travessia (`x`, sem leitura de diretório) para `thunder-panel` em `/root/.hermes` e `/root/.hermes/profiles/ygor-personal`, mais ACL `rwX` recursiva e ACL padrão nos diretórios do brain. **Condição adicional:** se `/root` ou qualquer ancestral intermediário for `0700`, ACL `x` também será necessária nesse ancestral ou um bind mount isolado; as ACLs solicitadas nos dois diretórios não bastam para atravessá-lo. Não tornar `/root` listável. Testar acessos efetivos, impacto em privacidade e herança; o brain deve permanecer isolado de outros usuários. `LoadCredential` monta cópias de credenciais no diretório privado do serviço, preservando conteúdo e hash do JSON; paths internos de evidências também exigem mapeamento verificado sem reescrever o JSON. Arquivos carregados como `0400` do UID efetivo devem respeitar os validadores do consumidor.
- Os candidatos em `runtime/systemd/*.candidate` não são units instaláveis agora: o modo T3 aceito no mesmo processo dispensa o broker para esta jornada, mas a opção separada ainda exige ensaio Linux. Os arquivos `TO-BE-REPLACED` em `LoadCredential` são placeholders intencionalmente inválidos; fontes e caminhos de evidências precisam ser obtidos por inventário e revisados antes de uso. `ProtectHome=off` no painel é apenas uma limitação do candidato por causa do layout sob `/root`, não uma prova de isolamento. `ProtectSystem=strict`, permissões Unix e `ReadWritePaths` não substituem a prova das necessidades reais do brain e subprocessos.
- O arquivo do broker `/etc/thunder-os/connectsafely-read-broker.env` deve ter somente `THUNDER_LI_BROKER_COMPANY`, `_ACCOUNT`, `_PUBLIC_ID`, `_PANEL_UID`, `_SOCKET_GID`, `_SOCKET` e `_CREDENTIAL_FILE` (cada sufixo com o prefixo completo `THUNDER_LI_BROKER`); o último é um caminho absoluto, nunca o valor da chave. O processo abre a chave sob demanda; ela deve ser arquivo regular sem symlink/hardlink, modo `0600` e proprietário `thunder-li-reader`. O arquivo de ambiente e a credencial exigem provisionamento separado e prova de negação de acesso ao painel.

## Runbook de implantação futura — bloqueado

### Ensaio candidato na VPS

O arquivo `scripts/rehearse_panel_non_root.sh` é candidato ainda não executado em Linux. Após revisão do Claude, disponibilizar uma cópia revisada desse script e uma release candidata imutável, compatível e de propriedade root na VPS. Definir os caminhos absolutos nas variáveis abaixo e executar na VPS:

```sh
sudo /bin/bash -p "$SCRIPT_ENSAIO" "$RELEASE_CANDIDATA"
```

Sem esses caminhos já preparados, o comando não está pronto para uso. O ensaio cria usuário e cópias descartáveis, isola a rede, usa credencial sintética e desliga efeitos externos. Verifica HTTP e gravação via CLI do mesmo painel com leitura de volta, não escrita HTTP; o teste root/WAL usa `umask 0007` controlado e não comprova o umask dos serviços reais. Se não conseguir provar parada e limpeza, preserva os artefatos para análise. Aprovar o ensaio exige execução real e revisão dos resultados.

**Não executar enquanto o pacote de acesso/credenciais não tiver revisão e ensaio em Linux, inventário efetivo completo e teste com todos os escritores.** Os comandos abaixo são roteiro, não autorização de implantação. Fazer janela coordenada, rollback previamente ensaiado e manter executor/timers de LinkedIn desligados; não alterar os flags de envio existentes sem análise da operação. A mudança de `User` **não** elimina a capacidade de envio por si.

1. **Pré-verificação somente leitura no host correto.**
   ```sh
   hostname
   systemctl show painel-thunder -p User -p Group -p FragmentPath -p DropInPaths -p NoNewPrivileges -p ProtectSystem
   systemctl show thunder-os-agent -p User -p Group -p FragmentPath -p DropInPaths
   namei -l /root/.hermes/profiles/ygor-personal/painel-dados/painel.db
   stat -c '%U %G %a %n' /root/.hermes/profiles/ygor-personal/painel-dados /root/.hermes/profiles/ygor-personal/painel-dados/painel.db
   ```
   Sucesso: identificar release, drop-ins, acesso por todos os diretórios e processos reais, sem imprimir variáveis com segredos. Identificar todos os demais escritores/configurações de modo separado; exemplo de unit não basta.
2. **Preparar identidades, após comprovar que não existem e não colidem.**
   ```sh
   getent passwd thunder-panel; getent passwd thunder-li-reader
   getent group thunder-panel-db; getent group thunder-li-socket
   sudo groupadd --system thunder-panel-db
   sudo groupadd --system thunder-li-socket
   sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin thunder-panel
   sudo useradd --system --gid thunder-li-socket --no-create-home --shell /usr/sbin/nologin thunder-li-reader
   sudo usermod -a -G thunder-panel-db,thunder-li-socket thunder-panel
   id thunder-panel; id thunder-li-reader
   ```
   Sucesso: UIDs distintos e não root, broker sem grupo do banco. Se identidade já existir, parar e conferir; não recriá-la nem assumir que pode ser reutilizada.
3. **Backup antes de alterar dono/permissões.** Coordenar todos os escritores e tirar backup consistente SQLite, sem copiar banco em WAL a quente como arquivo isolado. Exemplo, após definir um diretório protegido e novo em `BACKUP_DIR` e conferir espaço/permissões:
   ```sh
   sudo install -d -m 0700 "$BACKUP_DIR"
   sudo sqlite3 /root/.hermes/profiles/ygor-personal/painel-dados/painel.db ".backup '$BACKUP_DIR/painel.db'"
   sudo sqlite3 "$BACKUP_DIR/painel.db" 'PRAGMA integrity_check;'
   sudo sh -c 'umask 077; getfacl -p /root/.hermes/profiles/ygor-personal/painel-dados /root/.hermes/profiles/ygor-personal/painel-dados/painel.db > "$1/permissoes.acl"' sh "$BACKUP_DIR"
   sudo sh -c 'umask 077; systemctl cat painel-thunder > "$1/painel-thunder-unit.txt"' sh "$BACKUP_DIR"
   ```
   Sucesso: `integrity_check` retorna `ok`, backup e ACLs são legíveis apenas por root e foi testada restauração isolada. Não sobrescrever backup anterior.
4. **Preparar acessos e grupos somente após plano específico aprovado tecnicamente.** Corrigir os caminhos fixos/credenciais, acesso seletivo ao brain, socket nginx e umask/ACL dos escritores. Parar se não houver plano verificável para cada item. Com todos os escritores coordenados e backup conferido, os comandos abaixo ilustram apenas banco/diretório; caminhos ancestrais, credenciais e escritores ainda precisam de tratamento próprio:
   ```sh
   # Somente depois de testar a hipótese WAL no ensaio isolado:
   sudo chown root:thunder-panel-db /root/.hermes/profiles/ygor-personal/painel-dados
   sudo chown thunder-panel:thunder-panel-db /root/.hermes/profiles/ygor-personal/painel-dados/painel.db
   sudo chmod 2770 /root/.hermes/profiles/ygor-personal/painel-dados
   sudo chmod 0660 /root/.hermes/profiles/ygor-personal/painel-dados/painel.db
   # ACL rwX no brain: backup/restauração prévia das ACLs de toda a árvore é obrigatório.
   sudo setfacl -m u:thunder-panel:--x /root/.hermes /root/.hermes/profiles/ygor-personal
   sudo setfacl -R -m u:thunder-panel:rwX /root/.hermes/profiles/ygor-personal/brain
   sudo find -P /root/.hermes/profiles/ygor-personal/brain -type d -exec setfacl -m d:u:thunder-panel:rwX {} +
   ```
   Sucesso requer recriar WAL/SHM por cada escritor em staging e verificar modo/grupo, concorrência e bloqueios sem exposição ao broker. Se `/root` ou outro ancestral bloquear a travessia, conceder somente ACL `--x` nesse ancestral, após confirmar em `namei`, ou usar bind mount isolado; nunca conceder leitura de listagem a `/root`. O comando recursivo concede ao painel acesso amplo ao brain e precisa de revisão de privacidade, registro de todas as ACLs originais e rollback testado. Não adotar o exemplo enquanto não houver tratamento dos demais diretórios e arquivos existentes.
5. **Instalação somente após remover os bloqueios.** Preparar `CANDIDATE_DIR/painel.conf` com release imutável, fontes de `LoadCredential` e evidências revisadas; preservar os nove drop-ins existentes. No modo in-process aprovado, não se instala broker. Com pré-condições cumpridas:
   ```sh
   test ! -e /etc/systemd/system/painel-thunder.service.d/zzzz-non-root.conf
   sudo install -m 0644 "$CANDIDATE_DIR/painel.conf" /etc/systemd/system/painel-thunder.service.d/zzzz-non-root.conf
   sudo systemctl daemon-reload
   sudo systemctl restart painel-thunder.service
   ```
   Somente se a alternativa de broker for escolhida e revisada, instalar separadamente a unit e os dois arquivos de configuração, ensaiar launcher/UID e reiniciar o broker. Sucesso exige teste integral abaixo; `.candidate` não deve ser ativado.
6. **Verificação e parada se falhar.**
   ```sh
   systemctl show painel-thunder -p User -p Group -p MainPID -p NoNewPrivileges -p ProtectSystem
   sudo sh -c 'pid=$(systemctl show -p MainPID --value painel-thunder); grep -E "^(Uid|Gid|CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs):" /proc/$pid/status'
   sudo sh -c "stat -c '%U %G %a %n' /root/.hermes/profiles/ygor-personal/painel-dados/painel.db*"
   curl --silent --output /dev/null --write-out 'HTTP %{http_code}\n' http://127.0.0.1:8790/
   systemctl is-active thunder-os-agent.service
   ```
   Sucesso: para o painel, UID não root, capabilities efetivas/permitidas/ambiente e bounding nulas e `NoNewPrivs: 1`; painel responde pela rota autenticada, nginx e socket funcionam, prévia exibe `in_process_accepted` ao usar o adaptador do próprio painel. Se a opção de broker separado for escolhida, os UIDs devem ser distintos, a prévia lê pelo broker, o painel não lê a credencial do broker e o broker não lê credenciais de escrita; agente ainda grava e faz readback no mesmo banco, com WAL/SHM acessíveis aos escritores. Esses testes de jornada exigem comandos e dados de teste definidos para o ambiente; não há prova nesta rodada.
7. **Rollback, se qualquer verificação falhar.** Preservar evidências, parar broker novo, remover somente o drop-in que essa implantação criou, restaurar ACLs/donos conforme o inventário anterior (não assumir que todos já eram root), `daemon-reload`, reiniciar painel e validar painel e agente. Exemplo **somente após conferir os nomes concretos criados no passo 5**:
   ```sh
   sudo rm /etc/systemd/system/painel-thunder.service.d/zzzz-non-root.conf
   sudo setfacl --restore="$BACKUP_DIR/permissoes.acl"
   sudo systemctl daemon-reload
   sudo systemctl restart painel-thunder.service
   systemctl show painel-thunder -p User -p MainPID
   ```
   Validar também conteúdo e acesso ao banco; restaurar backup de banco somente mediante plano separado, pois poderia apagar escritas posteriores. Não remover drop-ins anteriores, usuários, dados ou credenciais.

## Riscos e lacunas

Acesso ao brain e `.env` sob `/root`, credenciais com caminho fixo, `PrivateTmp` e subprocessos Hermes podem quebrar o painel; `toques.py`/PostgreSQL pode perder acesso; nginx pode perder socket; outros escritores root podem criar WAL/SHM sem grupo compartilhado. O runbook exige inventário real, ajuste e teste em staging antes de mudança, backup e rollback verificados. Ainda faltam configuração/instalação de segredos e evidências, contrato de acesso às funções do painel e ensaio real das units; na opção de broker separado também falta ensaio Linux do launcher; portanto o pacote não está pronto para ser aplicado.
