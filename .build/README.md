# .build

Imagens Docker próprias usadas pelos apps desta loja. **Não é um app** — a
pasta começa com ponto pra o umbreld não tentar interpretá-la como tal.

## nerdminer (ativa)

Container único do `meuapps-nerdminer`: **cpuminer-opt + painel web +
supervisor**. Substituiu `cpuminer-sha` e `nerdminer-ui`, que ficam aqui só
como caminho de rollback (os workflows delas continuam funcionando).

**Por que um container só:** o cpuminer lê tudo por linha de comando e não
relê nada em runtime. Configurar pela UI significa reiniciar o processo com
argumentos novos — e um container não reinicia outro sem o socket do Docker,
que o Umbrel proíbe. Com o supervisor dentro do mesmo container, a UI
configura de verdade, e a config vive em `${APP_DATA_DIR}`, sobrevivendo aos
updates.

**Cuidados de memória** (depois do episódio de 24/09/2026, em que o vazamento
do cpuminer derrubou o Umbrel três vezes):

| Risco | Como está tratado |
|---|---|
| Vazamento do cpuminer no caminho GBT | `scantime` padrão 60s (12× menos que os 5s do upstream) + teto de memória no compose, que faz o kernel reiniciar o container em vez de derrubar a máquina |
| Histórico crescendo sem limite | todo buffer é `deque(maxlen=…)`; as médias usam soma+contagem, nunca listas acumuladas |
| Processo filho virando zumbi | um único `Popen` por vez, sempre com `wait()` depois de `terminate()`/`kill()` |
| Pipe do stdout enchendo e travando o miner | thread dedicada drenando até EOF, com as linhas indo pra um deque limitado e reimpressas pro `docker logs` |
| Threads de request presas em keep-alive | `timeout = 30` no handler, senão uma aba esquecida segura a thread pra sempre |
| Listeners do gráfico acumulando | `renderChart` remove os do desenho anterior antes de registrar novos |

**Patch do upstream:** o Dockerfile corrige um estouro de buffer no `api.c`
(`diff_str[16]` recebendo 23 bytes da dificuldade da mainnet, invadindo o
`algo` vizinho). Há `grep` antes e depois do `sed` pra o build **falhar** se
o upstream mudar a linha, em vez de aplicar um patch silenciosamente vazio.

**Limite que a UI não controla:** o teto de CPU — que na prática é o teto de
temperatura — fica no `docker-compose.yml`, porque um container não altera os
próprios limites. A UI controla threads e scantime, que operam dentro dele.

## ntp-ui

Painel web do `meuapps-ntp`. Mostra o desvio do relógio, stratum, fonte de
referência, último sync e a qualidade da sincronização, com gráfico histórico.
Python puro (stdlib), sem dependências.

**Por que fala o protocolo NTP em vez de usar `chronyc`:** o `startup.sh` da
imagem `cturra/ntp` não escreve `cmdallow`/`bindcmdaddress` na config, então a
porta de comando do chrony (323) só escuta em localhost — um container separado
não a alcança. O painel então monta pacotes NTP na mão (48 bytes, stdlib).

**Por que o desvio é medido contra fontes externas:** o painel e o chrony rodam
em containers do mesmo host, logo compartilham o relógio do kernel. Comparar um
com o outro daria offset zero sempre. Medir contra `a.st1.ntp.br`/`b.st1.ntp.br`
é o que responde de verdade "a hora daqui está certa?".

> ℹ️ **`chronyd -x`:** a imagem inicia o chrony com `-x`, que explicitamente
> **não ajusta o relógio do sistema**. Por isso o app não usa `cap_add:
> SYS_TIME` — não teria efeito. O container serve a hora do host e mede o
> desvio para reportar; quem sincroniza o host é o serviço de tempo do próprio
> umbrelOS.

## nerdminer-ui

Painel web do `meuapps-nerdminer`. Lê a API do cpuminer (TCP 4048, comando
`summary`), guarda 2h de histórico em memória e serve um dashboard HTML.
Python puro (stdlib), sem dependências.

Sobe como o serviço `ui` no compose do app; o `app_proxy` aponta pra ele, então
o botão **Open** finalmente abre algo — antes batia na API texto do cpuminer e
o navegador só mostrava `ECONNRESET`.

> ⚠️ **Pré-requisito no miner:** `--api-bind 0.0.0.0:4048`. No cpuminer esse
> parâmetro define **quem pode conectar e em que porta**, no formato
> `<ip>:<porta>` — e o literal `0.0.0.0` é o "aceita todos" (`ALLIP4` no
> `api.c`, que zera ip e máscara). Sem isso o padrão é só `127.0.0.1` e o
> painel, que fala de outro container, leva recusa.
>
> **Não existe a opção `--api-allow`** (foi tentada na 26.1.1 e derrubou o
> miner em loop de restart: *unrecognized option*). A 4048 não é publicada no
> host — só a rede interna do app alcança — e comandos remotos exigem
> `--api-remote`, que não usamos.

Campos que a API entrega: `NAME VER ALGO CPUS URL HS KHS ACC REJ SOL ACCMN
DIFF TEMP FAN FREQ UPTIME TS`. O `SOL` é o contador de blocos resolvidos — o
placar da loteria.

## cpuminer-sha

`cpuminer-opt` compilado com `-march=alderlake` (AVX2 + SHA-NI + VAES), para o
`meuapps-nerdminer`. É a receita do próprio autor para esse ISA
(`build-allarch.sh`, alvo `cpuminer-alderlake`) — e o ISA do N100.

> Tentativa que **não compila** (exit 2): `-march=x86-64-v3 -msha -mvaes`. Esse
> nível não inclui AES nem PCLMUL, que o código assume quando há AVX2.
> A imagem resultante só roda em Alder Lake+ — proposital.

As imagens públicas de cpuminer/cpuminer-opt são compiladas com
`-march=native` na máquina do mantenedor. Como esses runners não têm SHA-NI, o
binário sai sem ela e o sha256d roda na velocidade do AVX2 puro — mesmo num
CPU que tem a instrução. Medido no N100 do Umbrel:

Medido no N100 (1 thread, sem limite de CPU):

| Binário                                      | SW features            | Hashrate    | Temp |
| -------------------------------------------- | ---------------------- | ----------- | ---- |
| `cniweb/cpuminer-multi:1.3.7` (em uso antes) | —                      | ~5.6 MH/s   | —    |
| `cniweb/cpuminer-opt:25.1`                   | `AVX2 AES`             | ~5.55 MH/s  | 82°C |
| `cpuminer-sha` (esta)                        | `AVX2 VAES SHA256`     | **~26.6 MH/s** | 75°C |

**4,8× mais rápido e 7°C mais frio** — o hash sai do caminho das ALUs e vai
pro silício dedicado do SHA-NI.

> ⚠️ **Versão do upstream:** a tag `v27.4` do JayDDee está mal versionada — traz
> o código **24.7** (foi com ela que o primeiro build saiu). A última real é a
> **v26.1**, igual ao `master`. Conferir o `AC_INIT` do `configure.ac` na tag
> antes de mudar o `CPUMINER_VERSION`.

**Publicar:** Actions → *build cpuminer-sha* → Run workflow (ou push mexendo no
Dockerfile). No primeiro build, tornar o pacote público:
Packages → cpuminer-sha → Package settings → Change visibility → Public.

**Validar no destino** (o build não roda o binário de propósito — o runner
pode não ter SHA-NI e morreria com *Illegal instruction*):

```shellscript
sudo docker run --rm ghcr.io/herigson/cpuminer-sha:27.4 --benchmark -a sha256d -t 1
```

Conferir na saída: `SW features:` deve listar `SHA`, e
`Enabled optimizations:` deve mencionar SHA — não só AVX2.
