"""Planos de Teste: o que vai em cada lote cabe na cota por minuto do provedor mais apertado."""
import json
import unittest

from qa_testgen.ui.application import UserInterface


def _caso(n, requisitos):
    return {"titulo": f"CT{n:02d} HML - Validar integracao BI x HubSpot no cenario {n}",
            "pre_condicoes": "Cliente com fatura em aberto no BI/Lake",
            "passos": [{"numero": 1, "acao": "Executar a carga da integracao",
                        "resultado_esperado": "Propriedades atualizadas no HubSpot"}],
            "requisitos_relacionados": requisitos}


def _linha(n):
    return {"id": f"MC-{n:03d}", "funcionalidade": "Integracao BI x HubSpot", "requisito": f"RF{n:03d}",
            "cenario": f"Sincronizacao de cobranca consolidada {n}", "categoria": "Integração",
            "prioridade": "Alta", "criticidade": "Alta", "observacoes": "-"}


class MatrizDoLoteTests(unittest.TestCase):
    def test_leva_so_as_linhas_que_o_lote_referencia(self):
        matriz = [_linha(n) for n in range(1, 49)]
        lote = [_caso(n, [f"MC-{n:03d}"]) for n in range(1, 11)]
        do_lote = UserInterface._matriz_do_lote(matriz, lote)
        self.assertEqual([l["id"] for l in do_lote], [f"MC-{n:03d}" for n in range(1, 11)])

    def test_caso_que_cobre_varias_linhas_leva_todas(self):
        matriz = [_linha(n) for n in range(1, 6)]
        do_lote = UserInterface._matriz_do_lote(matriz, [_caso(1, ["MC-001", "MC-004"])])
        self.assertEqual([l["id"] for l in do_lote], ["MC-001", "MC-004"])

    def test_sem_referencia_manda_a_matriz_inteira(self):
        matriz = [_linha(n) for n in range(1, 6)]
        self.assertEqual(UserInterface._matriz_do_lote(matriz, [_caso(1, [])]), matriz)
        # id que não existe na matriz: idem, melhor mandar demais do que vazio
        self.assertEqual(UserInterface._matriz_do_lote(matriz, [_caso(1, ["MC-999"])]), matriz)

    def test_matriz_vazia_nao_quebra(self):
        self.assertEqual(UserInterface._matriz_do_lote([], [_caso(1, ["MC-001"])]), [])


class CotaPorMinutoTests(unittest.TestCase):
    """O caso real que falhou: 48 casos, 48 linhas de matriz, lotes de 10."""

    def setUp(self):
        self.matriz = [_linha(n) for n in range(1, 49)]
        self.casos = [_caso(n, [f"MC-{n:03d}"]) for n in range(1, 49)]
        self.tam = UserInterface._TAMANHO_LOTE_PLANOS
        self.lotes = [self.casos[i:i + self.tam] for i in range(0, len(self.casos), self.tam)]

    def _tokens(self, matriz, lote):
        return len(json.dumps({"m": matriz, "c": lote}, ensure_ascii=False)) // 4

    def test_filtrar_a_matriz_corta_o_payload_do_lote(self):
        lote = self.lotes[0]
        antes = self._tokens(self.matriz, lote)
        depois = self._tokens(UserInterface._matriz_do_lote(self.matriz, lote), lote)
        self.assertLess(depois * 2, antes, f"esperava menos da metade: {antes} -> {depois}")

    def test_todos_os_lotes_somados_cabem_na_janela_de_um_minuto(self):
        espera = UserInterface._espera_por_cota(self.matriz, self.lotes[0])
        por_lote = [self._tokens(UserInterface._matriz_do_lote(self.matriz, l), l) for l in self.lotes]
        # quantos lotes entram numa janela de 60s com essa espera, e quanto consomem
        cabem = int(60 // espera) + 1
        pior_janela = sum(sorted(por_lote, reverse=True)[:cabem])
        self.assertLessEqual(pior_janela, UserInterface._TOKENS_POR_MINUTO,
                             f"{cabem} lotes em 60s somam {pior_janela} tokens (espera={espera:.1f}s)")

    def test_espera_respeita_piso_e_teto(self):
        self.assertGreaterEqual(UserInterface._espera_por_cota([], [_caso(1, [])]), 5)
        enorme = [_linha(n) for n in range(1, 400)]
        self.assertLessEqual(UserInterface._espera_por_cota(enorme, [_caso(1, [])]), 30)


if __name__ == "__main__":
    unittest.main()
