"""
Espera entre lotes da geração (Matriz/Casos/Planos): curta depois de um
lote que deu certo, janela cheia do rate limit depois de um lote que falhou.
"""
import time
import unittest
from unittest import mock

import streamlit as st

from qa_testgen.ui.application import UserInterface


class _Estado(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v


class _Status:
    def __init__(self):
        self.labels, self.linhas = [], []

    def update(self, label=None, state=None):
        if label:
            self.labels.append(label)

    def write(self, texto):
        self.linhas.append(texto)


class _Rerun(Exception):
    pass


class BatchWaitTests(unittest.TestCase):
    def _rodar(self, respostas):
        """Executa o loop até o fim, registrando cada espera agendada (em segundos)."""
        host = UserInterface.__new__(UserInterface)
        host.state = _Estado()
        status = _Status()
        chamadas = iter(respostas)
        esperas = []

        def processar(lote):
            return next(chamadas)

        relogio = [1000.0]

        def agora():
            return relogio[0]

        def dormir(seg):
            relogio[0] += seg

        def rerun():
            raise _Rerun()

        with mock.patch.object(time, "time", agora), mock.patch.object(time, "sleep", dormir), \
                mock.patch.object(st, "rerun", rerun):
            for _ in range(200):
                try:
                    resultado = host._processar_um_lote_por_execucao("t", lambda: ["L1", "L2", "L3"], processar, status)
                except _Rerun:
                    prox = host.state.get("_t_proxima_liberacao")
                    if prox and (not esperas or esperas[-1][0] != prox):
                        esperas.append((prox, round(prox - relogio[0]), host.state.get("_t_motivo_espera")))
                    continue
                return resultado, [(e[1], e[2]) for e in esperas], status
        self.fail("loop não terminou")

    def test_short_wait_after_success_and_long_wait_after_error(self):
        respostas = [
            (["a"], None),                      # L1 ok  -> espera curta
            ([], "rate limit"),                 # L2 falha -> espera longa, tenta de novo
            (["b"], None),                      # L2 ok   -> espera curta
            (["c"], None),                      # L3 ok   -> fim
        ]
        (acumulado, erros), esperas, status = self._rodar(respostas)
        self.assertEqual(acumulado, ["a", "b", "c"])
        self.assertEqual(erros, [])
        self.assertEqual(esperas, [
            (UserInterface._ESPERA_ENTRE_LOTES_SEGUNDOS, None),
            (UserInterface._ESPERA_APOS_ERRO_SEGUNDOS, "erro"),
            (UserInterface._ESPERA_ENTRE_LOTES_SEGUNDOS, None),
        ])
        self.assertTrue(any("intervalo curto" in l for l in status.labels))
        self.assertTrue(any("lote anterior falhou" in l for l in status.labels))
        self.assertLess(UserInterface._ESPERA_ENTRE_LOTES_SEGUNDOS, 10)
        self.assertGreaterEqual(UserInterface._ESPERA_APOS_ERRO_SEGUNDOS, 60)

    def test_long_wait_before_next_batch_when_retries_exhausted(self):
        n = UserInterface._MAX_TENTATIVAS_POR_LOTE
        respostas = [([], "falhou")] * n + [(["x"], None), (["y"], None)]
        (acumulado, erros), esperas, _ = self._rodar(respostas)
        self.assertEqual(acumulado, ["x", "y"])
        self.assertEqual(len(erros), 1)
        # n-1 esperas longas entre tentativas + 1 longa antes do próximo lote + 1 curta depois de L2 ok
        self.assertEqual([e[0] for e in esperas], [UserInterface._ESPERA_APOS_ERRO_SEGUNDOS] * n + [UserInterface._ESPERA_ENTRE_LOTES_SEGUNDOS])
        self.assertIsNone(esperas[-1][1])


if __name__ == "__main__":
    unittest.main()
