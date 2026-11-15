import unittest

from sentinela import (BATERIA, BATERIA_BAIXA, ONLINE, Config, Estado,
                       pacote_magico)


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


class TestConfig(unittest.TestCase):
    def test_minimo(self):
        cfg = Config.do_ambiente({"SENTINELAS": "10.0.0.2, 10.0.0.3", "NUT_SENHA": "x"})
        self.assertEqual(cfg.sentinelas, ["10.0.0.2", "10.0.0.3"])
        self.assertEqual(cfg.controle, [])

    def test_exige_sentinelas_e_senha(self):
        with self.assertRaises(ValueError):
            Config.do_ambiente({"NUT_SENHA": "x"})
        with self.assertRaises(ValueError):
            Config.do_ambiente({"SENTINELAS": "10.0.0.2"})


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
