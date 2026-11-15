# sentinela

Um **nobreak virtual** que detecta falta de energia por ping e faz o TrueNAS
(ou qualquer cliente NUT) desligar com segurança, **sem precisar de nobreak
com USB**.

## Como funciona

- **Sentinelas** são equipamentos de rede ligados **direto na tomada**, sem
  nobreak (roteadores, access points, uma TV...). Quando a luz cai, eles somem
  da rede.
- O **controle** é um equipamento que está **no nobreak** junto com o
  servidor, normalmente o switch ou roteador por onde o NAS passa.
- O sentinela publica o estado como um nobreak de verdade pelo protocolo
  [NUT](https://networkupstools.org/) (porta 3493). O serviço de UPS do
  próprio TrueNAS, em modo remoto, enxerga esse "nobreak" e desliga o servidor
  do jeito oficial.

| Situação | Status publicado | O que acontece |
|---|---|---|
| Alguma sentinela responde | `OL` (na rede) | nada |
| Todas mudas há mais que `CARENCIA` | `OB` (na bateria) | alerta no TrueNAS |
| Mudas há `CARENCIA` + `AUTONOMIA` | `FSD OB LB` (bateria baixa) | TrueNAS desliga |
| Sentinelas **e** controle mudos | (congela) | nada: é problema de rede, não de luz |

Basta **uma** sentinela responder para considerar que há energia. Por isso um
roteador reiniciando para atualização não desliga nada. A carga
(`battery.charge`) cai de 100 a 0 durante a autonomia, então o TrueNAS mostra
uma "bateria" descendo.

## Instalação no TrueNAS

1. **Apps → Discover Apps → ⋮ → Install via YAML.** Cole o
   [`compose/truenas.yaml`](compose/truenas.yaml) e ajuste os IPs e a senha.
2. **System → Services → UPS**, configure e ative o serviço. Os nomes dos
   campos podem variar um pouco entre versões do TrueNAS:

   | Campo | Valor |
   |---|---|
   | UPS Mode | **Slave** (remoto/secundário) |
   | Identifier | `sentinela` |
   | Remote Host | `127.0.0.1` |
   | Remote Port | `3493` |
   | Monitor User / Password | os mesmos de `NUT_USUARIO` / `NUT_SENHA` |
   | Shutdown Mode | **UPS reaches low battery** |

   Com "UPS reaches low battery", quem decide o momento de desligar é o
   sentinela (`CARENCIA` + `AUTONOMIA`). Se preferir, "UPS goes on battery" +
   Shutdown Timer também funciona, mas aí o timer do TrueNAS manda.
3. Confira no Shell do TrueNAS: `upsc sentinela@127.0.0.1` deve mostrar
   `ups.status: OL`.

Para testar sem desligar nada, deixe o serviço de UPS desativado e
acompanhe os logs do app enquanto tira as sentinelas da tomada.

## Variáveis

| Variável | Padrão | Descrição |
|---|---|---|
| `SENTINELAS` | (obrigatória) | IPs sem nobreak, separados por vírgula |
| `CONTROLE` | vazio | IPs no nobreak; se todos sumirem, nada acontece |
| `NUT_SENHA` | (obrigatória) | senha do usuário NUT |
| `NUT_USUARIO` | `monitor` | usuário NUT |
| `UPS_NOME` | `sentinela` | nome do nobreak virtual |
| `CARENCIA` | `60` | segundos sem sentinelas até `OB` |
| `AUTONOMIA` | `240` | segundos em `OB` até bateria baixa |
| `INTERVALO` | `15` | segundos entre checagens |
| `PING_TIMEOUT` / `PING_TENTATIVAS` | `2` / `2` | por IP, a cada checagem |
| `PORTA` | `3493` | porta NUT |
| `WOL_MACS` | vazio | MACs para acordar quando a energia volta |
| `WOL_BROADCAST` | `255.255.255.255` | endereço de broadcast |
| `WOL_INTERVALO` / `WOL_DURACAO` | `120` / `1800` | repete o WoL por 30 min |
| `WOL_AO_INICIAR` | `false` | começa mandando WoL ao ligar |
| `LOG_NIVEL` | `INFO` | `DEBUG` para ver cada checagem |

## Religar quando a luz volta

Com o servidor desligado, nenhum app roda nele. Como o nobreak segurou a
energia o tempo todo, a opção "Restore on AC Power Loss" da BIOS também não
dispara. Você tem três caminhos:

- **Um roteador fora do nobreak manda Wake-on-LAN ao ligar.** Exemplo no
  MikroTik:
  `/system scheduler add name=acorda-nas start-time=startup interval=5m on-event="/tool wol mac=AA:BB:CC:DD:EE:FF interface=bridge"`.
  No OpenWrt: `sleep 180; etherwake -i br-lan AA:BB:CC:DD:EE:FF` no
  `/etc/rc.local`.
- **Um segundo sentinela num Raspberry Pi fora do nobreak**, com
  [`compose/raspberry-pi.yaml`](compose/raspberry-pi.yaml). Ele liga com a
  energia e manda WoL repetidas vezes (`WOL_AO_INICIAR`), e também quando
  detecta a volta da energia depois de uma falta.
- Deixar o nobreak descarregar até cortar a saída, e a BIOS religa o PC.

Repetir o WoL cobre o caso em que a luz volta enquanto o NAS ainda está
desligando. No servidor, ative Wake-on-LAN na BIOS e desative ErP/EuP.

## Desenvolvimento

```sh
python3 -m unittest -v          # testes da máquina de estados
docker build -t sentinela .
docker run --rm -e SENTINELAS=192.168.1.2 -e NUT_SENHA=x -p 3493:3493 sentinela
upsc sentinela@127.0.0.1
```

A cada push na `main`, o GitHub Actions roda os testes e publica a imagem em
`ghcr.io/leonardok/sentinela` para amd64, arm64 e armv7.

## Licença

GPL-3.0
