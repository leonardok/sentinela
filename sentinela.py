#!/usr/bin/env python3
"""Sentinela: um nobreak virtual que detecta falta de energia por ping.

"Sentinelas" são equipamentos de rede ligados direto na tomada (sem nobreak).
Se todos param de responder enquanto o "controle" (um equipamento que está no
nobreak) continua respondendo, é falta de luz.

O estado é publicado como um nobreak de verdade via NUT (Network UPS Tools),
usando o driver dummy-ups. Qualquer cliente NUT, como o serviço de UPS do
TrueNAS em modo remoto, enxerga e reage como faria com um nobreak real:

    OL         energia normal
    OB         "na bateria": sentinelas mudas há mais que CARENCIA segundos
    FSD OB LB  "bateria baixa": passou também AUTONOMIA segundos -> clientes desligam

Uma interface web (porta 8493) mostra o estado ao vivo e permite editar os IPs
e os tempos sem reiniciar. O que for salvo lá fica em /config/sentinela.json e
tem prioridade sobre as variáveis de ambiente.

Opcionalmente manda Wake-on-LAN quando a energia volta (útil rodando num
Raspberry Pi ou outro equipamento que não é o servidor que desliga).
"""

from __future__ import annotations

import base64
import dataclasses
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("sentinela")

ONLINE = "OL"
BATERIA = "OB"
# FSD (forced shutdown) faz clientes NUT em modo secundário, como o TrueNAS
# em modo remoto, desligarem na hora, sem esperar HOSTSYNC por um "primário".
BATERIA_BAIXA = "FSD OB LB"

PAINEL = Path(__file__).with_name("painel.html")


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

def _lista(valor) -> list[str]:
    if isinstance(valor, list):
        itens = valor
    else:
        itens = str(valor).replace(";", ",").replace("\n", ",").split(",")
    return [str(item).strip() for item in itens if str(item).strip()]


def _bool(valor) -> bool:
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in {"1", "true", "sim", "yes", "on"}


@dataclass
class Config:
    sentinelas: list[str] = field(default_factory=list)
    controle: list[str] = field(default_factory=list)
    intervalo: float = 15
    carencia: float = 60
    autonomia: float = 240
    ping_timeout: int = 2
    ping_tentativas: int = 2
    wol_macs: list[str] = field(default_factory=list)
    wol_broadcast: str = "255.255.255.255"
    wol_duracao: float = 1800
    wol_intervalo: float = 120
    wol_ao_iniciar: bool = False
    # Fixos (só por variável de ambiente)
    ups_nome: str = "sentinela"
    nut_usuario: str = "monitor"
    nut_senha: str = ""
    porta: int = 3493
    ui_porta: int = 8493
    ui_senha: str = ""
    config_arquivo: Path = Path("/config/sentinela.json")
    conf_dir: Path = Path("/etc/nut")
    estado_dir: Path = Path("/run/nut")

    @classmethod
    def do_ambiente(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        senha = env.get("NUT_SENHA", "")
        if not senha:
            raise ValueError("defina NUT_SENHA (senha que o TrueNAS vai usar para conectar)")
        base = cls(
            ups_nome=env.get("UPS_NOME", "sentinela"),
            nut_usuario=env.get("NUT_USUARIO", "monitor"),
            nut_senha=senha,
            porta=int(env.get("PORTA", 3493)),
            ui_porta=int(env.get("UI_PORTA", 8493)),
            ui_senha=env.get("UI_SENHA", ""),
            config_arquivo=Path(env.get("CONFIG_ARQUIVO", "/config/sentinela.json")),
            conf_dir=Path(env.get("NUT_CONF_DIR", "/etc/nut")),
            estado_dir=Path(env.get("NUT_ESTADO_DIR", "/run/nut")),
        )
        mapa = {
            "SENTINELAS": "sentinelas", "CONTROLE": "controle", "INTERVALO": "intervalo",
            "CARENCIA": "carencia", "AUTONOMIA": "autonomia", "PING_TIMEOUT": "ping_timeout",
            "PING_TENTATIVAS": "ping_tentativas", "WOL_MACS": "wol_macs",
            "WOL_BROADCAST": "wol_broadcast", "WOL_DURACAO": "wol_duracao",
            "WOL_INTERVALO": "wol_intervalo", "WOL_AO_INICIAR": "wol_ao_iniciar",
        }
        ajustes = {campo: env[var] for var, campo in mapa.items() if var in env}
        return base.com(validar_ajustes(ajustes, base.ajustes()))

    def ajustes(self) -> dict:
        return {nome: getattr(self, nome) for nome in AJUSTAVEIS}

    def com(self, ajustes: dict) -> "Config":
        return dataclasses.replace(self, **ajustes)

    @property
    def senha_painel(self) -> str:
        return self.ui_senha or self.nut_senha


class ErroValidacao(ValueError):
    def __init__(self, erros: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in erros.items()))
        self.erros = erros


_HOST = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")
_MAC = re.compile(r"^[0-9a-fA-F]{2}([:-]?[0-9a-fA-F]{2}){5}$")


def _endereco(valor: str) -> bool:
    try:
        ipaddress.ip_address(valor)
        return True
    except ValueError:
        return bool(_HOST.match(valor)) and not valor.replace(".", "").isdigit()


def _numero(minimo: float, maximo: float, inteiro: bool = False):
    def conv(valor):
        n = float(valor)
        if n != n or not (minimo <= n <= maximo):
            raise ValueError(f"deve ficar entre {minimo:g} e {maximo:g}")
        return int(n) if inteiro else n
    return conv


def _enderecos(valor):
    itens = _lista(valor)
    ruins = [i for i in itens if not _endereco(i)]
    if ruins:
        raise ValueError(f"endereço inválido: {', '.join(ruins)}")
    if len(set(itens)) != len(itens):
        raise ValueError("há endereços repetidos")
    return itens


def _macs(valor):
    itens = _lista(valor)
    ruins = [i for i in itens if not _MAC.match(i)]
    if ruins:
        raise ValueError(f"MAC inválido: {', '.join(ruins)}")
    return [m.upper().replace("-", ":") if len(m) == 17 else
            ":".join(m[i:i + 2] for i in range(0, 12, 2)).upper() for m in itens]


def _broadcast(valor):
    try:
        return str(ipaddress.IPv4Address(str(valor).strip()))
    except ValueError:
        raise ValueError("use um IPv4, ex.: 192.168.1.255") from None


AJUSTAVEIS = {
    "sentinelas": _enderecos,
    "controle": _enderecos,
    "intervalo": _numero(2, 3600),
    "carencia": _numero(0, 86400),
    "autonomia": _numero(0, 86400),
    "ping_timeout": _numero(1, 30, inteiro=True),
    "ping_tentativas": _numero(1, 10, inteiro=True),
    "wol_macs": _macs,
    "wol_broadcast": _broadcast,
    "wol_duracao": _numero(0, 86400),
    "wol_intervalo": _numero(10, 86400),
    "wol_ao_iniciar": _bool,
}


def validar_ajustes(dados: dict, atuais: dict | None = None) -> dict:
    """Valida e normaliza ajustes; devolve só os conhecidos. Levanta ErroValidacao."""
    resultado = dict(atuais or {})
    erros = {}
    for nome, valor in dados.items():
        conv = AJUSTAVEIS.get(nome)
        if conv is None:
            continue
        try:
            resultado[nome] = conv(valor)
        except (TypeError, ValueError) as erro:
            erros[nome] = str(erro) or "valor inválido"
    if erros:
        raise ErroValidacao(erros)
    sobreposto = set(resultado.get("sentinelas", [])) & set(resultado.get("controle", []))
    if sobreposto:
        raise ErroValidacao({"controle": f"não pode repetir uma sentinela: {', '.join(sorted(sobreposto))}"})
    return resultado


def carregar_salvo(cfg: Config) -> Config:
    try:
        dados = json.loads(cfg.config_arquivo.read_text())
    except FileNotFoundError:
        return cfg
    except (OSError, json.JSONDecodeError) as erro:
        log.error("não consegui ler %s (%s); usando variáveis de ambiente", cfg.config_arquivo, erro)
        return cfg
    try:
        return cfg.com(validar_ajustes(dados, cfg.ajustes()))
    except ErroValidacao as erro:
        log.error("configuração salva inválida (%s); usando variáveis de ambiente", erro)
        return cfg


def salvar(cfg: Config) -> None:
    cfg.config_arquivo.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.config_arquivo.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.ajustes(), indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, cfg.config_arquivo)


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
        self.rede_ruim = False

    def atualizar(self, dt: float, sentinela_ok: bool, controle_ok: bool) -> Leitura:
        anterior = self.status
        evento = None

        if sentinela_ok:
            self.sem_energia = 0.0
            self.status = ONLINE
            self.rede_ruim = False
            if anterior != ONLINE:
                evento = "voltou"
        elif not controle_ok:
            if not self.rede_ruim:
                evento = "rede"
            self.rede_ruim = True
        else:
            self.rede_ruim = False
            self.sem_energia += dt
            self._reclassificar()
            if self.status != anterior:
                evento = "bateria_baixa" if self.status == BATERIA_BAIXA else "falta"

        return self.leitura(evento)

    def _reclassificar(self) -> None:
        if self.sem_energia <= 0:
            self.status = ONLINE
        elif self.sem_energia >= self.carencia + self.autonomia:
            self.status = BATERIA_BAIXA
        elif self.sem_energia >= self.carencia:
            self.status = BATERIA
        else:
            self.status = ONLINE

    def ajustar(self, carencia: float, autonomia: float) -> None:
        """Novos tempos valem já, sem perder o tempo sem energia acumulado."""
        self.carencia, self.autonomia = carencia, autonomia
        if self.sem_energia > 0:
            self._reclassificar()

    def leitura(self, evento: str | None = None) -> Leitura:
        return Leitura(self.status, self.carga(), self.restante(), evento)

    def restante(self) -> int:
        if self.status == ONLINE and self.sem_energia == 0:
            return int(self.autonomia)
        return max(0, int(self.carencia + self.autonomia - max(self.sem_energia, self.carencia)))

    def carga(self) -> int:
        if self.status == ONLINE:
            return 100
        if self.autonomia <= 0:
            return 0
        return max(0, min(100, round(100 * self.restante() / self.autonomia)))


# --------------------------------------------------------------------------
# Rede
# --------------------------------------------------------------------------

_TEMPO = re.compile(r"time[=<]([\d.]+)\s*ms")


def pingar(ip: str, timeout: int, tentativas: int) -> float | None:
    """Latência em ms, ou None se não respondeu."""
    for _ in range(max(1, tentativas)):
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), ip],
                capture_output=True, text=True, timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0:
            m = _TEMPO.search(r.stdout)
            return float(m.group(1)) if m else 0.0
    return None


def pingar_todos(ips: list[str], timeout: int, tentativas: int) -> dict[str, float | None]:
    if not ips:
        return {}
    with ThreadPoolExecutor(max_workers=min(32, len(ips))) as pool:
        return dict(zip(ips, pool.map(lambda ip: pingar(ip, timeout, tentativas), ips)))


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
        f"sentinela.alvos: {' '.join(cfg.sentinelas) or '-'}",
        f"sentinela.controle: {' '.join(cfg.controle) or '-'}",
        "",
    ])


def escrever_dev(caminho: Path, conteudo: str) -> None:
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(conteudo)
    os.replace(tmp, caminho)


def caminho_dev(cfg: Config) -> Path:
    return cfg.conf_dir / f"{cfg.ups_nome}.dev"


def gerar_configs(cfg: Config) -> Path:
    cfg.conf_dir.mkdir(parents=True, exist_ok=True)
    cfg.estado_dir.mkdir(parents=True, exist_ok=True)
    dev = caminho_dev(cfg)
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
# Monitor: junta rede, estado, NUT e Wake-on-LAN
# --------------------------------------------------------------------------

MENSAGENS = {
    "falta": ("aviso", "Falta de energia: sentinelas mudas, nobreak virtual em bateria"),
    "bateria_baixa": ("perigo", "Bateria baixa: clientes NUT vão desligar agora"),
    "voltou": ("ok", "Energia voltou"),
    "rede": ("aviso", "Sentinelas e controle mudos: problema de rede, não de energia; aguardando"),
}


class Monitor:
    def __init__(self, cfg: Config, pinger=pingar_todos, escrever=True):
        self.cfg = cfg
        self.estado = Estado(cfg.carencia, cfg.autonomia)
        self.trava = threading.RLock()
        self.acordar = threading.Event()
        self.pinger = pinger
        self.escrever = escrever
        self.alvos: dict[str, dict] = {}
        self.ultima_checagem: float | None = None
        self.eventos: deque = deque(maxlen=50)
        self.iniciado = time.time()
        self._ultimo_tick = time.monotonic()
        self._ultimo_dev = ""
        self._wol_ate = time.monotonic() + cfg.wol_duracao if (cfg.wol_macs and cfg.wol_ao_iniciar) else 0.0
        self._proximo_wol = 0.0
        self.registrar("info", "Sentinela iniciado")

    def registrar(self, nivel: str, mensagem: str) -> None:
        self.eventos.appendleft({"quando": datetime.now().astimezone().isoformat(timespec="seconds"),
                                 "nivel": nivel, "mensagem": mensagem})
        (log.warning if nivel in {"aviso", "perigo"} else log.info)("%s", mensagem)

    def checar(self) -> Leitura:
        with self.trava:
            cfg = self.cfg
        resultados = self.pinger(cfg.sentinelas + cfg.controle, cfg.ping_timeout, cfg.ping_tentativas)
        agora_parede = time.time()

        with self.trava:
            if self.cfg is not cfg:      # config mudou durante o ping: descarta esta rodada
                return self.estado.leitura()
            for ip in cfg.sentinelas + cfg.controle:
                ms = resultados.get(ip)
                anterior = self.alvos.get(ip, {})
                self.alvos[ip] = {
                    "ok": ms is not None,
                    "ms": ms,
                    "desde": anterior.get("desde") if anterior.get("ok") == (ms is not None) else agora_parede,
                }
            agora = time.monotonic()
            dt, self._ultimo_tick = agora - self._ultimo_tick, agora
            self.ultima_checagem = agora_parede

            if cfg.sentinelas:
                sentinela_ok = any(resultados.get(ip) is not None for ip in cfg.sentinelas)
                controle_ok = (not cfg.controle) or any(resultados.get(ip) is not None for ip in cfg.controle)
                leitura = self.estado.atualizar(dt, sentinela_ok, controle_ok)
            else:
                leitura = self.estado.atualizar(dt, True, True)  # sem sentinelas: nunca dispara

            if leitura.evento in MENSAGENS:
                self.registrar(*MENSAGENS[leitura.evento])
            if leitura.evento == "voltou" and cfg.wol_macs:
                self._wol_ate = agora + cfg.wol_duracao
                self._proximo_wol = 0.0

            self._publicar(leitura)
            self._talvez_wol(agora, leitura)
            return leitura

    def _publicar(self, leitura: Leitura) -> None:
        conteudo = conteudo_dev(leitura, self.cfg)
        if self.escrever and conteudo != self._ultimo_dev:
            escrever_dev(caminho_dev(self.cfg), conteudo)
        self._ultimo_dev = conteudo

    def _talvez_wol(self, agora: float, leitura: Leitura) -> None:
        cfg = self.cfg
        if cfg.wol_macs and agora < self._wol_ate and agora >= self._proximo_wol and leitura.status == ONLINE:
            try:
                enviar_wol(cfg.wol_macs, cfg.wol_broadcast)
            except OSError as erro:
                log.error("falha ao enviar Wake-on-LAN: %s", erro)
            self._proximo_wol = agora + cfg.wol_intervalo

    def atualizar_config(self, dados: dict) -> Config:
        with self.trava:
            novos = validar_ajustes(dados, self.cfg.ajustes())
            novo = self.cfg.com(novos)
            salvar(novo)
            self.cfg = novo
            self.estado.ajustar(novo.carencia, novo.autonomia)
            validos = set(novo.sentinelas + novo.controle)
            self.alvos = {ip: v for ip, v in self.alvos.items() if ip in validos}
            if not novo.sentinelas:
                self.estado.sem_energia = 0.0
                self.estado.status = ONLINE
            self._publicar(self.estado.leitura())
            self.registrar("info", "Configuração atualizada pela interface")
        self.acordar.set()
        return novo

    def snapshot(self) -> dict:
        with self.trava:
            cfg, est = self.cfg, self.estado
            leitura = est.leitura()

            def alvos(ips):
                return [{"ip": ip, **self.alvos.get(ip, {"ok": None, "ms": None, "desde": None})} for ip in ips]

            return {
                "status": leitura.status,
                "situacao": self._situacao(),
                "carga": leitura.carga,
                "restante": leitura.autonomia,
                "sem_energia": round(est.sem_energia, 1),
                "carencia": cfg.carencia,
                "autonomia": cfg.autonomia,
                "rede_ruim": est.rede_ruim,
                "sentinelas": alvos(cfg.sentinelas),
                "controle": alvos(cfg.controle),
                "ultima_checagem": self.ultima_checagem,
                "intervalo": cfg.intervalo,
                "ups": {"nome": cfg.ups_nome, "porta": cfg.porta, "usuario": cfg.nut_usuario},
                "eventos": list(self.eventos),
                "iniciado": self.iniciado,
            }

    def _situacao(self) -> str:
        if not self.cfg.sentinelas:
            return "sem_sentinelas"
        if self.ultima_checagem is None:
            return "iniciando"
        if self.estado.rede_ruim:
            return "rede"
        if self.estado.status == BATERIA_BAIXA:
            return "desligando"
        if self.estado.status == BATERIA:
            return "bateria"
        if self.estado.sem_energia > 0:
            return "carencia"
        return "normal"

    def loop(self) -> None:
        while True:
            inicio = time.monotonic()
            try:
                self.checar()
            except Exception:  # noqa: BLE001 - o monitor nunca pode morrer por um erro pontual
                log.exception("erro na checagem")
            self.acordar.wait(max(0.0, self.cfg.intervalo - (time.monotonic() - inicio)))
            self.acordar.clear()


# --------------------------------------------------------------------------
# Interface web
# --------------------------------------------------------------------------

def criar_servidor(monitor: Monitor, host: str = "0.0.0.0", porta: int | None = None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "sentinela"

        def log_message(self, formato, *args):
            log.debug("http %s - %s", self.address_string(), formato % args)

        def _autorizado(self) -> bool:
            senha = monitor.cfg.senha_painel
            cabecalho = self.headers.get("Authorization", "")
            if cabecalho.startswith("Basic "):
                try:
                    _, _, recebida = base64.b64decode(cabecalho[6:]).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    recebida = ""
                if hmac.compare_digest(recebida.encode(), senha.encode()):
                    return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="sentinela", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _enviar(self, codigo: int, corpo: bytes, tipo: str) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            self.wfile.write(corpo)

        def _json(self, codigo: int, dados) -> None:
            self._enviar(codigo, json.dumps(dados, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            caminho = self.path.split("?", 1)[0]
            if caminho == "/saude":
                return self._enviar(200, b"ok\n", "text/plain")
            if not self._autorizado():
                return
            if caminho == "/":
                return self._enviar(200, PAINEL.read_bytes(), "text/html; charset=utf-8")
            if caminho == "/api/estado":
                return self._json(200, monitor.snapshot())
            if caminho == "/api/config":
                with monitor.trava:
                    return self._json(200, monitor.cfg.ajustes())
            self._json(404, {"erro": "não encontrado"})

        def do_POST(self):
            if not self._autorizado():
                return
            if self.path.split("?", 1)[0] != "/api/config":
                return self._json(404, {"erro": "não encontrado"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"erro": "envie JSON"})
            try:
                tamanho = int(self.headers.get("Content-Length", 0))
                dados = json.loads(self.rfile.read(min(tamanho, 65536)) or b"{}")
                if not isinstance(dados, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"erro": "JSON inválido"})
            try:
                novo = monitor.atualizar_config(dados)
            except ErroValidacao as erro:
                return self._json(400, {"erros": erro.erros})
            except OSError as erro:
                return self._json(500, {"erro": f"não consegui salvar: {erro}"})
            self._json(200, novo.ajustes())

    return ThreadingHTTPServer((host, monitor.cfg.ui_porta if porta is None else porta), Handler)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_NIVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = carregar_salvo(Config.do_ambiente())
    except ValueError as erro:
        log.error("%s", erro)
        return 2

    gerar_configs(cfg)
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

    monitor = Monitor(cfg)
    servidor = criar_servidor(monitor)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    threading.Thread(target=monitor.loop, daemon=True).start()
    log.info("NUT na porta %s, interface web na porta %s", cfg.porta, cfg.ui_porta)
    if not cfg.sentinelas:
        log.warning("nenhuma sentinela configurada: abra a interface web para configurar")

    while True:
        for p in processos:
            if p.poll() is not None:
                log.error("processo do NUT saiu (%s, código %s); encerrando", p.args[0], p.returncode)
                encerrar()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
