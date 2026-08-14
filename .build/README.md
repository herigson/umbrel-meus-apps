# .build

Imagens Docker próprias usadas pelos apps desta loja. **Não é um app** — a
pasta começa com ponto pra o umbreld não tentar interpretá-la como tal.

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
