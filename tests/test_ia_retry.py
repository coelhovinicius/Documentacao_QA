"""
Regra única de retentativa das chamadas de IA (ui/ia_retry.py):
chamada única com _chamar_ia_com_retentativas e loop de imagens sem espera
entre unidades que deram certo.
"""
import time
import unittest
from unittest import mock

import streamlit as st

from qa_testgen.ui.ia_retry import IaRetryMixin


class _Estado(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v

    def delete(self, k):
        self.pop(k, None)


class _Status:
    def __init__(self):
        self.labels, self.linhas = [], []

    def update(self, label=None, state=None, expanded=None):
        if label:
            self.labels.append(label)

    def write(self, texto):
        self.linhas.append(texto)


class _Rerun(Exception):
    pass


class _Host(IaRetryMixin):
    def __init__(self):
        self.state = _Estado()
        self.acoes = []

    def trigger_action(self, nome):
        self.acoes.append(nome)
        self.state.set('current_action', nome)


def _rodar(fn, host, prefixo):
    """Executa fn() repetidamente (simulando os reruns) até devolver algo != None; registra as esperas agendadas."""
    relogio = [1000.0]
    esperas = []

    def agora():
        relogio[0] += 0.001   # o relógio anda um pouco a cada leitura (senão duas esperas de 0s ficariam iguais)
        return relogio[0]

    def dormir(seg):
        relogio[0] += seg

    def rerun():
        raise _Rerun()

    with mock.patch.object(time, "time", agora), mock.patch.object(time, "sleep", dormir), mock.patch.object(st, "rerun", rerun):
        for _ in range(500):
            try:
                r = fn()
            except _Rerun:
                prox = host.state.get(f"_{prefixo}_proxima_liberacao")
                if prox and (not esperas or esperas[-1][0] != prox):
                    esperas.append((prox, round(prox - relogio[0])))
                continue
            return r, [e[1] for e in esperas]
    raise AssertionError("não terminou")


class ChamadaUnicaTests(unittest.TestCase):
    def _executar(self, respostas, prefixo="t"):
        host = _Host()
        status = _Status()
        chamadas = iter(respostas)
        montagens = []

        def montar():
            montagens.append(1)
            return {"x": 1}

        def chamar(p):
            r = next(chamadas)
            if isinstance(r, Exception):
                raise r
            return r

        resultado, esperas = _rodar(lambda: host._chamar_ia_com_retentativas(prefixo, montar, chamar, status), host, prefixo)
        return resultado, esperas, status, host, montagens

    def test_success_first_try_has_no_wait(self):
        (payload, resp, erro), esperas, status, host, montagens = self._executar([{"ok": True}])
        self.assertEqual(payload, {"x": 1})
        self.assertEqual(resp, {"ok": True})
        self.assertIsNone(erro)
        self.assertEqual(esperas, [])
        self.assertEqual(len(montagens), 1)
        self.assertIsNone(host.state.get("_t_lotes_pendentes"))   # estado limpo no fim

    def test_error_waits_full_window_then_succeeds_and_prep_runs_once(self):
        (payload, resp, erro), esperas, status, host, montagens = self._executar([RuntimeError("rate limit"), {"ok": 2}])
        self.assertEqual(resp, {"ok": 2})
        self.assertIsNone(erro)
        self.assertEqual(esperas, [IaRetryMixin._ESPERA_APOS_ERRO_SEGUNDOS])
        self.assertEqual(len(montagens), 1)   # preparação não se repete nos reruns da espera
        self.assertTrue(any("tentando de novo" in l for l in status.linhas))
        self.assertTrue(any("antes de tentar de novo" in l for l in status.labels))
        self.assertTrue(any("A chamada à IA falhou" in l for l in status.linhas))

    def test_exhausted_retries_return_error(self):
        n = IaRetryMixin._MAX_TENTATIVAS_POR_LOTE
        (payload, resp, erro), esperas, status, host, _ = self._executar([RuntimeError("boom")] * n)
        self.assertIsNone(resp)
        self.assertEqual(payload, {"x": 1})
        self.assertIn("boom", erro)
        self.assertEqual(esperas, [IaRetryMixin._ESPERA_APOS_ERRO_SEGUNDOS] * (n - 1))
        self.assertIsNone(host.state.get("_t_lotes_pendentes"))

    def test_prep_failure_is_immediate_without_retry(self):
        host = _Host()

        def montar():
            raise RuntimeError("Azure fora")

        r = host._chamar_ia_com_retentativas("p", montar, lambda p: {"ok": 1}, _Status())
        self.assertEqual(r, (None, None, "Azure fora"))
        self.assertIsNone(host.state.get("_p_lotes_pendentes"))

    def test_iniciar_limpa_varios_prefixos_e_prep(self):
        host = _Host()
        host.state.set("_a_lotes_pendentes", [1])
        host.state.set("_b_prep", {"x": 1})
        host.state.set("_b_motivo_espera", "erro")
        host._iniciar_geracao_em_lotes("acao", ("a", "b"))
        self.assertEqual(host.acoes, ["acao"])
        for k in ("_a_lotes_pendentes", "_b_prep", "_b_motivo_espera"):
            self.assertNotIn(k, host.state)


class LoopSemEsperaTests(unittest.TestCase):
    def test_images_no_wait_between_successes_but_wait_after_error(self):
        host = _Host()
        status = _Status()
        respostas = iter([(["d1"], None), ([], "falhou"), (["d2"], None), (["d3"], None)])

        (acumulado, erros), esperas = _rodar(
            lambda: host._processar_um_lote_por_execucao("img", lambda: ["i1", "i2", "i3"], lambda i: next(respostas), status,
                                                         nome_item="imagem", espera_entre_lotes=0), host, "img")
        self.assertEqual(acumulado, ["d1", "d2", "d3"])
        self.assertEqual(erros, [])
        self.assertEqual(esperas, [0, IaRetryMixin._ESPERA_APOS_ERRO_SEGUNDOS, 0])
        self.assertTrue(any("Imagem 2 de 3 falhou" in l for l in status.linhas))
        self.assertTrue(any("Processando imagem 3 de 3" in l for l in status.labels))


if __name__ == "__main__":
    unittest.main()
