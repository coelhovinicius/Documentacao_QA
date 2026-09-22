"""Chave global que liga/desliga a leitura das imagens dos documentos pela IA."""
import unittest
from types import SimpleNamespace

import streamlit as st

from qa_testgen.infrastructure.document_store import CONFIG_INTERPRETAR_IMAGENS
from qa_testgen.ui.application import UserInterface


class _Estado:
    def get(self, k, default=None):
        return st.session_state.get(k, default)

    def set(self, k, v):
        st.session_state[k] = v


class _Falso:
    """Exercita o leitor da configuração sem subir a UI inteira."""

    _interpretar_imagens_ligado = UserInterface._interpretar_imagens_ligado

    def __init__(self, valor_no_banco=None, turso=True):
        self.config = SimpleNamespace(turso_database_url="libsql://x" if turso else "", turso_auth_token="t")
        self.state = _Estado()
        self._valor = valor_no_banco


class InterpretarImagensTests(unittest.TestCase):
    def setUp(self):
        st.session_state.clear()

    def _com_banco(self, valor):
        """Simula o AppSettingsStore devolvendo `valor` pra chave."""
        import qa_testgen.ui.application as app

        class _Store:
            def __init__(self, *a, **k):
                pass

            def ensure_schema(self):
                pass

            def get(self, chave, default=None):
                assert chave == CONFIG_INTERPRETAR_IMAGENS, chave
                return valor if valor is not None else default

        original = app.AppSettingsStore
        app.AppSettingsStore = _Store
        self.addCleanup(lambda: setattr(app, "AppSettingsStore", original))

    def test_padrao_e_ligado(self):
        self._com_banco(None)
        self.assertTrue(_Falso()._interpretar_imagens_ligado())

    def test_zero_desliga(self):
        self._com_banco("0")
        self.assertFalse(_Falso()._interpretar_imagens_ligado())

    def test_um_liga(self):
        self._com_banco("1")
        self.assertTrue(_Falso()._interpretar_imagens_ligado())

    def test_sem_turso_fica_ligado(self):
        self.assertTrue(_Falso(turso=False)._interpretar_imagens_ligado())

    def test_le_uma_vez_por_sessao(self):
        self._com_banco("0")
        ui = _Falso()
        self.assertFalse(ui._interpretar_imagens_ligado())
        # troca o valor no "banco": a sessão em andamento mantém o que já leu
        self._com_banco("1")
        self.assertFalse(ui._interpretar_imagens_ligado())

    def test_falha_no_banco_nao_derruba_o_passo_1(self):
        import qa_testgen.ui.application as app

        class _StoreQuebrado:
            def __init__(self, *a, **k):
                raise RuntimeError("Turso fora do ar")

        original = app.AppSettingsStore
        app.AppSettingsStore = _StoreQuebrado
        self.addCleanup(lambda: setattr(app, "AppSettingsStore", original))
        self.assertTrue(_Falso()._interpretar_imagens_ligado())


if __name__ == "__main__":
    unittest.main()
