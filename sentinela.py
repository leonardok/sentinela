#!/usr/bin/env python3
"""Sentinela: um nobreak virtual que detecta falta de energia por ping.

"Sentinelas" são equipamentos de rede ligados direto na tomada (sem nobreak).
Se todos param de responder enquanto o "controle" (um equipamento que está no
nobreak) continua respondendo, é falta de luz.

O estado é publicado como um nobreak de verdade via NUT (Network UPS Tools),
usando o driver dummy-ups. Qualquer cliente NUT, como o serviço de UPS do
TrueNAS em modo remoto, enxerga e reage como faria com um nobreak real:

    OL      energia normal
    OB      "na bateria": sentinelas mudas há mais que CARENCIA segundos
    FSD OB LB  "bateria baixa": passou também AUTONOMIA segundos -> clientes desligam

Opcionalmente manda Wake-on-LAN quando a energia volta (útil rodando num
Raspberry Pi ou outro equipamento que não é o servidor que desliga).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("sentinela")

ONLINE = "OL"
BATERIA = "OB"
# FSD (forced shutdown) faz clientes NUT em modo secundário, como o TrueNAS
# em modo remoto, desligarem na hora, sem esperar HOSTSYNC por um "primário".
BATERIA_BAIXA = "FSD OB LB"


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

def _lista(valor: str) -> list[str]:
    return [item.strip() for item in valor.replace(";", ",").split(",") if item.strip()]


def _bool(valor: str) -> bool:
    return valor.strip().lower() in {"1", "true", "sim", "yes", "on"}


@dataclass
class Config:
    sentinelas: list[str]
    controle: list[str] = field(default_factory=list)
    intervalo: float = 15
    carencia: float = 60
    autonomia: float = 240
    ups_nome: str = "sentinela"
    nut_usuario: str = "monitor"
    nut_senha: str = ""
    porta: int = 3493
    ping_timeout: int = 2
    ping_tentativas: int = 2
    wol_macs: list[str] = field(default_factory=list)
    wol_broadcast: str = "255.255.255.255"
    wol_duracao: float = 1800
    wol_intervalo: float = 120
    wol_ao_iniciar: bool = False
    conf_dir: Path = Path("/etc/nut")
    estado_dir: Path = Path("/run/nut")

    @classmethod
    def do_ambiente(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        sentinelas = _lista(env.get("SENTINELAS", ""))
        if not sentinelas:
            raise ValueError("defina SENTINELAS com ao menos um IP (separados por vírgula)")
        senha = env.get("NUT_SENHA", "")
        if not senha:
            raise ValueError("defina NUT_SENHA (senha que o TrueNAS vai usar para conectar)")
        return cls(
            sentinelas=sentinelas,
            controle=_lista(env.get("CONTROLE", "")),
            intervalo=float(env.get("INTERVALO", 15)),
            carencia=float(env.get("CARENCIA", 60)),
            autonomia=float(env.get("AUTONOMIA", 240)),
            ups_nome=env.get("UPS_NOME", "sentinela"),
            nut_usuario=env.get("NUT_USUARIO", "monitor"),
            nut_senha=senha,
            porta=int(env.get("PORTA", 3493)),
            ping_timeout=int(env.get("PING_TIMEOUT", 2)),
            ping_tentativas=int(env.get("PING_TENTATIVAS", 2)),
            wol_macs=_lista(env.get("WOL_MACS", "")),
            wol_broadcast=env.get("WOL_BROADCAST", "255.255.255.255"),
            wol_duracao=float(env.get("WOL_DURACAO", 1800)),
            wol_intervalo=float(env.get("WOL_INTERVALO", 120)),
            wol_ao_iniciar=_bool(env.get("WOL_AO_INICIAR", "false")),
            conf_dir=Path(env.get("NUT_CONF_DIR", "/etc/nut")),
            estado_dir=Path(env.get("NUT_ESTADO_DIR", "/run/nut")),
        )


# --------------------------------------------------------------------------
# Máquina de estados (pura, sem rede: fácil de testar)
# --------------------------------------------------------------------------

@dataclass
class Leitura:
    status: str
    carga: int          # battery.charge, 0-100
    autonomia: int      # battery.runtime, segundos até "bateria baixa"
    evento: str | None  # "falta", "bateria_baixa", "voltou", "rede" ou None


class Estado:
    """Decide o status do nobreak virtual a cada checagem.

    O tempo sem energia só acumula quando a evidência é confiável: todas as
    sentinelas mudas E o controle respondendo. Se o controle também some, o
    problema é na rede do próprio servidor; o relógio pausa e nada muda.
    """

    def __init__(self, carencia: float, autonomia: float):
        self.carencia = carencia
        self.autonomia = autonomia
        self.sem_energia = 0.0
        self.status = ONLINE
        self._rede_ruim = False

    def atualizar(self, dt: float, sentinela_ok: bool, controle_ok: bool) -> Leitura:
        anterior = self.status
        evento = None

        if sentinela_ok:
            self.sem_energia = 0.0
            self.status = ONLINE
            self._rede_ruim = False
            if anterior != ONLINE:
                evento = "voltou"
        elif not controle_ok:
            if not self._rede_ruim:
                evento = "rede"
            self._rede_ruim = True
        else:
            self._rede_ruim = False
            self.sem_energia += dt
            if self.sem_energia >= self.carencia + self.autonomia:
                self.status = BATERIA_BAIXA
            elif self.sem_energia >= self.carencia:
                self.status = BATERIA
            if self.status != anterior:
                evento = "bateria_baixa" if self.status == BATERIA_BAIXA else "falta"

        return Leitura(self.status, self.carga(), self.restante(), evento)

    def restante(self) -> int:
        if self.status == ONLINE:
            return int(self.autonomia)
        return max(0, int(self.carencia + self.autonomia - self.sem_energia))

    def carga(self) -> int:
        if self.status == ONLINE or self.autonomia <= 0:
            return 100 if self.status == ONLINE else 0
        return max(0, min(100, round(100 * self.restante() / self.autonomia)))


# --------------------------------------------------------------------------
# Rede
# --------------------------------------------------------------------------

def responde(ip: str, timeout: int, tentativas: int) -> bool:
    for _ in range(max(1, tentativas)):
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            return True
    return False


def algum_responde(ips: list[str], timeout: int, tentativas: int) -> bool:
    if not ips:
        return False
    with ThreadPoolExecutor(max_workers=len(ips)) as pool:
        return any(pool.map(lambda ip: responde(ip, timeout, tentativas), ips))


def pacote_magico(mac: str) -> bytes:
    hexa = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(hexa) != 12:
        raise ValueError(f"MAC inválido: {mac}")
    return b"\xff" * 6 + bytes.fromhex(hexa) * 16


def enviar_wol(macs: list[str], broadcast: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            for porta in (9, 7):
                s.sendto(pacote_magico(mac), (broadcast, porta))
    log.info("Wake-on-LAN enviado para %s", ", ".join(macs))


# --------------------------------------------------------------------------
# NUT
# --------------------------------------------------------------------------

def conteudo_dev(leitura: Leitura, cfg: Config) -> str:
    return "\n".join([
        f"ups.status: {leitura.status}",
        f"battery.charge: {leitura.carga}",
        "battery.charge.low: 0",
        f"battery.runtime: {leitura.autonomia}",
        "device.mfr: Sentinela",
        "device.model: Nobreak virtual por ping",
        "device.type: ups",
        "ups.mfr: Sentinela",
        "ups.model: Nobreak virtual por ping",
        f"sentinela.alvos: {' '.join(cfg.sentinelas)}",
        f"sentinela.controle: {' '.join(cfg.controle) or '-'}",
        "",
    ])


def escrever_dev(caminho: Path, conteudo: str) -> None:
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(conteudo)
    os.replace(tmp, caminho)


def gerar_configs(cfg: Config) -> Path:
    cfg.conf_dir.mkdir(parents=True, exist_ok=True)
    cfg.estado_dir.mkdir(parents=True, exist_ok=True)
    dev = cfg.conf_dir / f"{cfg.ups_nome}.dev"
    (cfg.conf_dir / "nut.conf").write_text("MODE=netserver\n")
    (cfg.conf_dir / "ups.conf").write_text(
        f"[{cfg.ups_nome}]\n"
        "  driver = dummy-ups\n"
        f"  port = {dev}\n"
        '  desc = "Sentinela: nobreak virtual por ping"\n'
    )
    (cfg.conf_dir / "upsd.conf").write_text(f"LISTEN 0.0.0.0 {cfg.porta}\n")
    usuarios = cfg.conf_dir / "upsd.users"
    usuarios.write_text(
        f"[{cfg.nut_usuario}]\n"
        f"  password = {cfg.nut_senha}\n"
        "  upsmon secondary\n"
    )
    usuarios.chmod(0o640)
    escrever_dev(dev, conteudo_dev(Leitura(ONLINE, 100, int(cfg.autonomia), None), cfg))
    return dev


def iniciar_nut(cfg: Config) -> list[subprocess.Popen]:
    env = dict(os.environ, NUT_CONFPATH=str(cfg.conf_dir), NUT_STATEPATH=str(cfg.estado_dir))
    usuario = ["-u", "nut"] if os.geteuid() == 0 else []
    driver = subprocess.Popen(["/lib/nut/dummy-ups", "-F", "-a", cfg.ups_nome, *usuario], env=env)
    time.sleep(1)
    upsd = subprocess.Popen(["/lib/nut/upsd", "-F", *usuario], env=env)
    return [driver, upsd]


# --------------------------------------------------------------------------
# Loop principal
# --------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_NIVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = Config.do_ambiente()
    except ValueError as erro:
        log.error("%s", erro)
        return 2

    dev = gerar_configs(cfg)
    if os.geteuid() == 0:
        subprocess.run(["chown", "-R", "nut:nut", str(cfg.estado_dir)], check=False)
        subprocess.run(["chgrp", "nut", str(cfg.conf_dir / "upsd.users")], check=False)
    processos = iniciar_nut(cfg)

    def encerrar(*_):
        for p in processos:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)

    log.info(
        "sentinelas=%s controle=%s carência=%ss autonomia=%ss porta=%s",
        ",".join(cfg.sentinelas), ",".join(cfg.controle) or "-",
        cfg.carencia, cfg.autonomia, cfg.porta,
    )

    estado = Estado(cfg.carencia, cfg.autonomia)
    wol_ate = time.monotonic() + cfg.wol_duracao if (cfg.wol_macs and cfg.wol_ao_iniciar) else 0.0
    proximo_wol = 0.0
    ultimo = time.monotonic()
    ultimo_conteudo = ""

    while True:
        for p in processos:
            if p.poll() is not None:
                log.error("processo do NUT saiu (%s, código %s); encerrando", p.args[0], p.returncode)
                encerrar()
                return 1

        sentinela_ok = algum_responde(cfg.sentinelas, cfg.ping_timeout, cfg.ping_tentativas)
        controle_ok = True
        if not sentinela_ok and cfg.controle:
            controle_ok = algum_responde(cfg.controle, cfg.ping_timeout, cfg.ping_tentativas)

        agora = time.monotonic()
        leitura = estado.atualizar(agora - ultimo, sentinela_ok, controle_ok)
        ultimo = agora

        if leitura.evento == "falta":
            log.warning("FALTA DE ENERGIA: sentinelas mudas, nobreak virtual em bateria")
        elif leitura.evento == "bateria_baixa":
            log.warning("BATERIA BAIXA: clientes NUT vão desligar agora")
        elif leitura.evento == "voltou":
            log.warning("ENERGIA VOLTOU")
            if cfg.wol_macs:
                wol_ate = agora + cfg.wol_duracao
                proximo_wol = 0.0
        elif leitura.evento == "rede":
            log.warning("sentinelas e controle mudos: problema de rede, não de energia; aguardando")
        elif not sentinela_ok and controle_ok:
            log.info("sentinelas mudas há %.0fs", estado.sem_energia)

        conteudo = conteudo_dev(leitura, cfg)
        if conteudo != ultimo_conteudo:
            escrever_dev(dev, conteudo)
            ultimo_conteudo = conteudo

        if cfg.wol_macs and agora < wol_ate and agora >= proximo_wol and leitura.status == ONLINE:
            try:
                enviar_wol(cfg.wol_macs, cfg.wol_broadcast)
            except OSError as erro:
                log.error("falha ao enviar Wake-on-LAN: %s", erro)
            proximo_wol = agora + cfg.wol_intervalo

        time.sleep(max(0.0, cfg.intervalo - (time.monotonic() - agora)))


if __name__ == "__main__":
    sys.exit(main())
