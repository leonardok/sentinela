import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sentinela import (BATERIA, BATERIA_BAIXA, ONLINE, Config, ErroValidacao,
                       Estado, Monitor, carregar_salvo, criar_servidor,
                       pacote_magico, salvar, validar_ajustes)


class TestEstado(unittest.TestCase):
    def setUp(self):
        self.e = Estado(carencia=60, autonomia=240)

    def passos(self, n, dt=15, sentinela=False, controle=True):
        leitura = None
        for _ in range(n):
            leitura = self.e.atualizar(dt, sentinela, controle)
        return leitura

    def test_energia_normal(self):
        leitura = self.passos(10, sentinela=True)
        self.assertEqual(leitura.status, ONLINE)
        self.assertEqual(leitura.carga, 100)

    def test_carencia_antes_de_ir_para_bateria(self):
        self.assertEqual(self.passos(3).status, ONLINE)  # 45s
        leitura = self.passos(1)                          # 60s
        self.assertEqual(leitura.status, BATERIA)
        self.assertEqual(leitura.evento, "falta")
        self.assertEqual(leitura.carga, 100)

    def test_bateria_baixa_apos_autonomia(self):
        self.passos(4)                                    # 60s -> OB
        leitura = self.passos(8)                          # 180s
        self.assertEqual(leitura.status, BATERIA)
        self.assertEqual(leitura.carga, 50)
        self.assertEqual(leitura.autonomia, 120)
        leitura = self.passos(8)                          # 300s
        self.assertEqual(leitura.status, BATERIA_BAIXA)
        self.assertEqual(leitura.evento, "bateria_baixa")
        self.assertEqual(leitura.carga, 0)

    def test_uma_sentinela_volta_zera_tudo(self):
        self.passos(10)
        leitura = self.passos(1, sentinela=True)
        self.assertEqual(leitura.status, ONLINE)
        self.assertEqual(leitura.evento, "voltou")
        self.assertEqual(self.e.sem_energia, 0)

    def test_problema_de_rede_nao_conta_tempo(self):
        self.passos(3)                                    # 45s acumulados
        leitura = self.passos(100, controle=False)
        self.assertEqual(leitura.status, ONLINE)
        self.assertEqual(self.e.sem_energia, 45)

    def test_problema_de_rede_durante_falta_congela(self):
        self.passos(6)                                    # 90s, OB
        leitura = self.passos(100, controle=False)
        self.assertEqual(leitura.status, BATERIA)
        self.assertEqual(self.e.sem_energia, 90)

    def test_evento_de_rede_so_uma_vez(self):
        eventos = [self.e.atualizar(15, False, False).evento for _ in range(5)]
        self.assertEqual(eventos, ["rede", None, None, None, None])

    def test_baixa_permanece_ate_voltar(self):
        self.passos(40)
        self.assertEqual(self.passos(40).status, BATERIA_BAIXA)

    def test_ajustar_tempos_no_meio_da_falta(self):
        self.passos(6)                                    # 90s, OB
        self.e.ajustar(carencia=30, autonomia=30)
        self.assertEqual(self.e.status, BATERIA_BAIXA)
        self.e.ajustar(carencia=600, autonomia=600)
        self.assertEqual(self.e.status, ONLINE)
        self.assertEqual(self.e.sem_energia, 90)


class TestValidacao(unittest.TestCase):
    def test_normaliza(self):
        r = validar_ajustes({
            "sentinelas": "10.0.0.2, roteador.lan\n10.0.0.3",
            "carencia": "30", "ping_tentativas": 3.0,
            "wol_macs": ["aa-bb-cc-dd-ee-ff", "112233445566"],
            "wol_ao_iniciar": "sim", "desconhecido": 1,
        })
        self.assertEqual(r["sentinelas"], ["10.0.0.2", "roteador.lan", "10.0.0.3"])
        self.assertEqual(r["carencia"], 30.0)
        self.assertEqual(r["ping_tentativas"], 3)
        self.assertEqual(r["wol_macs"], ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"])
        self.assertTrue(r["wol_ao_iniciar"])
        self.assertNotIn("desconhecido", r)

    def test_erros_por_campo(self):
        with self.assertRaises(ErroValidacao) as c:
            validar_ajustes({"sentinelas": ["10.0.0.300", "ok.lan"], "intervalo": 0,
                             "wol_macs": ["xx"], "carencia": "abc", "wol_broadcast": "rede"})
        self.assertEqual(set(c.exception.erros),
                         {"sentinelas", "intervalo", "wol_macs", "carencia", "wol_broadcast"})
        self.assertIn("10.0.0.300", c.exception.erros["sentinelas"])

    def test_repetidos_e_sobreposicao(self):
        with self.assertRaises(ErroValidacao):
            validar_ajustes({"sentinelas": ["10.0.0.2", "10.0.0.2"]})
        with self.assertRaises(ErroValidacao) as c:
            validar_ajustes({"sentinelas": ["10.0.0.2"], "controle": ["10.0.0.2"]})
        self.assertIn("controle", c.exception.erros)

    def test_tempos_do_nobreak(self):
        r = validar_ajustes({"nobreak_duracao": "180", "tempo_desligar": 90})
        self.assertEqual((r["nobreak_duracao"], r["tempo_desligar"]), (180.0, 90.0))
        with self.assertRaises(ErroValidacao):
            validar_ajustes({"tempo_desligar": -1})

    def test_padroes_cabem_em_nobreak_curto(self):
        cfg = Config()
        self.assertLessEqual(cfg.carencia + cfg.autonomia, 120)

    def test_ipv6(self):
        self.assertEqual(validar_ajustes({"controle": ["fe80::1"]})["controle"], ["fe80::1"])


class TestConfig(unittest.TestCase):
    def test_ambiente(self):
        cfg = Config.do_ambiente({"SENTINELAS": "10.0.0.2, 10.0.0.3", "NUT_SENHA": "x", "CARENCIA": "10"})
        self.assertEqual(cfg.sentinelas, ["10.0.0.2", "10.0.0.3"])
        self.assertEqual(cfg.carencia, 10)
        self.assertEqual(cfg.senha_painel, "x")

    def test_sentinelas_opcionais_senha_obrigatoria(self):
        self.assertEqual(Config.do_ambiente({"NUT_SENHA": "x"}).sentinelas, [])
        with self.assertRaises(ValueError):
            Config.do_ambiente({"SENTINELAS": "10.0.0.2"})

    def test_ambiente_invalido(self):
        with self.assertRaises(ErroValidacao):
            Config.do_ambiente({"NUT_SENHA": "x", "SENTINELAS": "não é ip"})

    def test_salvo_tem_prioridade(self):
        with tempfile.TemporaryDirectory() as d:
            arq = Path(d) / "c" / "sentinela.json"
            cfg = Config(sentinelas=["10.0.0.9"], nut_senha="x", config_arquivo=arq)
            self.assertEqual(carregar_salvo(cfg).sentinelas, ["10.0.0.9"])
            salvar(cfg.com({"sentinelas": ["10.0.0.2"], "carencia": 5.0}))
            carregado = carregar_salvo(cfg)
            self.assertEqual(carregado.sentinelas, ["10.0.0.2"])
            self.assertEqual(carregado.carencia, 5.0)
            self.assertEqual(carregado.nut_senha, "x")
            arq.write_text("{quebrado")
            self.assertEqual(carregar_salvo(cfg).sentinelas, ["10.0.0.9"])


class PingFalso:
    def __init__(self):
        self.vivos = set()

    def __call__(self, ips, timeout, tentativas):
        return {ip: (1.5 if ip in self.vivos else None) for ip in ips}


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ping = PingFalso()
        cfg = Config(sentinelas=["10.0.0.2", "10.0.0.3"], controle=["10.0.0.1"], carencia=0.05,
                     autonomia=0.05, nut_senha="x", config_arquivo=Path(self.tmp.name) / "s.json")
        self.m = Monitor(cfg, pinger=self.ping, escrever=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ciclo(self):
        import time
        self.ping.vivos = {"10.0.0.2", "10.0.0.1"}
        self.assertEqual(self.m.checar().status, ONLINE)
        self.assertEqual(self.m.snapshot()["situacao"], "normal")
        self.ping.vivos = {"10.0.0.1"}
        for _ in range(4):
            time.sleep(0.04)
            self.m.checar()
        snap = self.m.snapshot()
        self.assertEqual(snap["status"], BATERIA_BAIXA)
        self.assertEqual(snap["situacao"], "desligando")
        self.assertFalse(snap["sentinelas"][0]["ok"])
        self.assertTrue(snap["controle"][0]["ok"])
        self.assertIn("FSD OB LB", self.m._ultimo_dev)
        mensagens = [e["mensagem"] for e in snap["eventos"]]
        self.assertTrue(any("Bateria baixa" in m for m in mensagens))

    def test_sem_sentinelas_nunca_dispara(self):
        self.m.atualizar_config({"sentinelas": [], "controle": []})
        for _ in range(3):
            self.assertEqual(self.m.checar().status, ONLINE)
        self.assertEqual(self.m.snapshot()["situacao"], "sem_sentinelas")

    def test_atualizar_config_persiste(self):
        self.m.atualizar_config({"sentinelas": ["10.0.0.7"], "carencia": 99})
        salvo = json.loads(self.m.cfg.config_arquivo.read_text())
        self.assertEqual(salvo["sentinelas"], ["10.0.0.7"])
        self.assertEqual(salvo["carencia"], 99)
        self.assertNotIn("nut_senha", salvo)
        self.assertEqual(self.m.estado.carencia, 99)


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Config(sentinelas=["10.0.0.2"], nut_senha="segredo",
                     config_arquivo=Path(cls.tmp.name) / "s.json")
        cls.monitor = Monitor(cfg, pinger=PingFalso(), escrever=False)
        cls.srv = criar_servidor(cls.monitor, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def req(self, caminho, dados=None, senha="segredo", tipo="application/json"):
        r = urllib.request.Request(self.base + caminho, method="POST" if dados is not None else "GET")
        if senha is not None:
            r.add_header("Authorization", "Basic " + base64.b64encode(f"admin:{senha}".encode()).decode())
        if dados is not None:
            r.add_header("Content-Type", tipo)
            r.data = json.dumps(dados).encode()
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_saude_sem_senha(self):
        self.assertEqual(self.req("/saude", senha=None)[0], 200)

    def test_exige_senha(self):
        self.assertEqual(self.req("/", senha=None)[0], 401)
        self.assertEqual(self.req("/api/estado", senha="errada")[0], 401)

    def test_painel_e_estado(self):
        codigo, corpo = self.req("/")
        self.assertEqual(codigo, 200)
        self.assertIn(b"Sentinela", corpo)
        codigo, corpo = self.req("/api/estado")
        self.assertEqual(json.loads(corpo)["sentinelas"][0]["ip"], "10.0.0.2")

    def test_salvar_config(self):
        codigo, corpo = self.req("/api/config", {"sentinelas": ["10.0.0.5", "10.0.0.6"], "autonomia": 120})
        self.assertEqual(codigo, 200, corpo)
        self.assertEqual(json.loads(corpo)["sentinelas"], ["10.0.0.5", "10.0.0.6"])
        self.assertEqual(json.loads(self.req("/api/config")[1])["autonomia"], 120)

    def test_erro_de_validacao(self):
        codigo, corpo = self.req("/api/config", {"sentinelas": ["999.1.1.1"]})
        self.assertEqual(codigo, 400)
        self.assertIn("sentinelas", json.loads(corpo)["erros"])

    def test_exige_json(self):
        self.assertEqual(self.req("/api/config", {"a": 1}, tipo="text/plain")[0], 415)


class TestWol(unittest.TestCase):
    def test_pacote(self):
        p = pacote_magico("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(p), 102)
        self.assertTrue(p.startswith(b"\xff" * 6 + bytes.fromhex("aabbccddeeff")))

    def test_mac_invalido(self):
        with self.assertRaises(ValueError):
            pacote_magico("AA:BB")


if __name__ == "__main__":
    unittest.main()
