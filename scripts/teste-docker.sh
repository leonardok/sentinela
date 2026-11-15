#!/usr/bin/env bash
# Teste de ponta a ponta do sentinela, só com Docker.
#
# Sobe numa rede Docker isolada:
#   - 2 "sentinelas" e 1 "controle" (containers parados, só para responder ping)
#   - o sentinela, vigiando esses três
#   - um "TrueNAS de mentira": upsmon em modo secundário, como o serviço de UPS
#     do TrueNAS, com um comando de desligamento que só cria um arquivo
# e simula: uma sentinela caindo, problema de rede e falta de energia.
#
# Uso:  ./scripts/teste-docker.sh            (constrói a imagem a partir do repo)
#       IMG=ghcr.io/leonardok/sentinela:latest SKIP_BUILD=1 ./scripts/teste-docker.sh
set -euo pipefail

IMG="${IMG:-sentinela:teste}"
P="st$$"                         # prefixo único para containers e rede
REDE="$P-rede"
SUB="${SUB:-172.31.250}"
IP_S1="$SUB.11" IP_S2="$SUB.12" IP_CTRL="$SUB.13" IP_SENT="$SUB.20" IP_CLI="$SUB.30"
SENHA="teste123"

vermelho() { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
passo() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

limpar() {
  docker rm -f "$P-s1" "$P-s2" "$P-ctrl" "$P-sentinela" "$P-cliente" >/dev/null 2>&1 || true
  docker network rm "$REDE" >/dev/null 2>&1 || true
}
falhar() {
  vermelho "FALHOU: $*"
  echo "--- logs do sentinela"; docker logs "$P-sentinela" 2>&1 | tail -40 || true
  echo "--- logs do cliente";   docker logs "$P-cliente" 2>&1 | tail -20 || true
  exit 1
}
trap limpar EXIT

status() { docker exec "$P-sentinela" upsc sentinela@127.0.0.1 ups.status 2>/dev/null | tail -n 1; }
api() {
  docker exec "$P-sentinela" python3 -c "
import urllib.request, base64, sys
r = urllib.request.Request('http://127.0.0.1:8493' + sys.argv[1])
r.add_header('Authorization', 'Basic ' + base64.b64encode(b'x:$SENHA').decode())
print(urllib.request.urlopen(r).read().decode())" "$1"
}
esperar_status() {   # esperar_status <esperado> <segundos>
  local fim=$((SECONDS + $2)) s=""
  while (( SECONDS < fim )); do
    s="$(status || true)"
    [[ "$s" == "$1" ]] && { verde "status = $s"; return 0; }
    sleep 1
  done
  falhar "esperava status '$1' em $2 s, ficou '$s'"
}
manter_status() {    # manter_status <esperado> <segundos>: não pode mudar nesse tempo
  local fim=$((SECONDS + $2)) s=""
  while (( SECONDS < fim )); do
    s="$(status || true)"
    [[ "$s" == "$1" ]] || falhar "status deveria continuar '$1', virou '$s'"
    sleep 1
  done
  verde "status continuou $1 por $2 s"
}
desligar_rede() { docker network disconnect "$REDE" "$P-$1"; }
ligar_rede() { docker network connect --ip "$2" "$REDE" "$P-$1"; }

if [[ -z "${SKIP_BUILD:-}" ]]; then
  passo "Construindo $IMG"
  docker build -q -t "$IMG" "$(dirname "$0")/.." >/dev/null
fi

passo "Subindo a rede de teste"
limpar
docker network create --subnet "$SUB.0/24" "$REDE" >/dev/null
for alvo in "s1:$IP_S1" "s2:$IP_S2" "ctrl:$IP_CTRL"; do
  docker run -d --name "$P-${alvo%%:*}" --network "$REDE" --ip "${alvo#*:}" \
    --entrypoint sleep "$IMG" infinity >/dev/null
done

docker run -d --name "$P-sentinela" --network "$REDE" --ip "$IP_SENT" --cap-add NET_RAW \
  -e NUT_SENHA="$SENHA" \
  -e SENTINELAS="$IP_S1,$IP_S2" -e CONTROLE="$IP_CTRL" \
  -e CARENCIA=4s -e AUTONOMIA=6s -e INTERVALO=2s -e PING_TIMEOUT=1 -e PING_TENTATIVAS=1 \
  "$IMG" >/dev/null

docker run -d --name "$P-cliente" --network "$REDE" --ip "$IP_CLI" --entrypoint bash "$IMG" -c "
  mkdir -p /run/nut && chown nut:nut /run/nut
  cat > /etc/nut/upsmon.conf <<EOF
MONITOR sentinela@$IP_SENT:3493 1 monitor $SENHA secondary
MINSUPPLIES 1
SHUTDOWNCMD \"/bin/touch /tmp/desligou\"
POLLFREQ 1
POLLFREQALERT 1
FINALDELAY 0
POWERDOWNFLAG /tmp/killpower
EOF
  chmod 640 /etc/nut/upsmon.conf; chgrp nut /etc/nut/upsmon.conf
  sleep 3
  /lib/nut/upsmon -D
  sleep infinity" >/dev/null

passo "1. Tudo ligado"
esperar_status OL 30
config="$(api /api/config)"
grep -q '"travados": \[.*"sentinelas"' <<<"$config" || falhar "SENTINELAS do ambiente deveria estar travada na interface: $config"
verde "interface responde e mostra as variáveis do app travadas"
[[ "$(docker exec "$P-sentinela" python3 -c "
import urllib.request as u, urllib.error as e
try: u.urlopen('http://127.0.0.1:8493/'); print('aberto')
except e.HTTPError as x: print(x.code)")" == "401" ]] || falhar "interface deveria exigir senha"
verde "interface exige senha"

passo "2. Uma sentinela cai (a outra continua respondendo)"
desligar_rede s1
manter_status OL 8

passo "3. Sentinela e controle mudos (problema de rede, não de luz)"
desligar_rede s2; desligar_rede ctrl
manter_status OL 12

passo "4. Controle volta, sentinelas continuam mudas (falta de luz)"
ligar_rede ctrl "$IP_CTRL"
esperar_status OB 15
esperar_status "FSD OB LB" 15

passo "5. O cliente NUT (como o TrueNAS) desliga"
fim=$((SECONDS + 20))
until docker exec "$P-cliente" test -f /tmp/desligou; do
  (( SECONDS < fim )) || falhar "o upsmon não executou o comando de desligamento"
  sleep 1
done
verde "upsmon executou o desligamento"
docker logs "$P-cliente" 2>&1 | grep -E "on battery|battery is low|forced shutdown|Executing automatic" | sed 's/^/   /'

passo "6. Energia volta"
ligar_rede s1 "$IP_S1"
esperar_status OL 15
api /api/estado | grep -q '"Energia voltou"' || falhar "evento 'Energia voltou' não registrado"
verde "evento registrado na interface"

printf '\n'; verde "TODOS OS TESTES PASSARAM"
