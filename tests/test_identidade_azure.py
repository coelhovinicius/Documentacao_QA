"""Tag criado-por: nome do usuário no Azure, com o mapa de identidades do dono."""
import unittest
from types import SimpleNamespace

import streamlit as st

from qa_testgen.ui.application import UserInterface


class _Falso:
    """Só o necessário pra exercitar os dois métodos (sem subir a UI inteira)."""

    def __init__(self, turso=""):
        self.config = SimpleNamespace(turso_database_url=turso, turso_auth_token="")

    _identidade_azure = UserInterface._identidade_azure
    _tag_criado_por = UserInterface._tag_criado_por


class IdentidadeAzureTests(unittest.TestCase):
    def setUp(self):
        st.session_state.clear()
        st.session_state["auth_user"] = "admin"

    def test_sem_mapa_usa_o_usuario_do_login(self):
        st.session_state["identidades_azure_cache"] = {}
        self.assertEqual(_Falso()._tag_criado_por(), "criado-por:admin")

    def test_mapa_troca_o_nome_que_vai_pro_azure(self):
        st.session_state["identidades_azure_cache"] = {"admin": "vinicius"}
        self.assertEqual(_Falso()._tag_criado_por(), "criado-por:vinicius")

    def test_preserva_tags_existentes_e_normaliza(self):
        st.session_state["identidades_azure_cache"] = {"admin": "Vinicius Bemfica"}
        tag = _Falso()._tag_criado_por("Frontend 360; Pesquisa de Pulso")
        self.assertEqual(tag, "Frontend 360; Pesquisa de Pulso; criado-por:vinicius-bemfica")

    def test_username_explicito_ignora_sessao(self):
        # é assim que os fluxos em thread recebem a tag já pronta
        st.session_state["identidades_azure_cache"] = {"admin": "vinicius"}
        self.assertEqual(_Falso()._tag_criado_por(username="jonatas.soares"), "criado-por:jonatas.soares")

    def test_sem_sessao_nao_fica_desconhecido_quando_ha_mapa(self):
        st.session_state.pop("auth_user", None)
        st.session_state["identidades_azure_cache"] = {"": ""}
        self.assertEqual(_Falso()._tag_criado_por(), "criado-por:desconhecido")

    def test_outro_usuario_sem_mapa_mantem_o_proprio_login(self):
        st.session_state["auth_user"] = "jonatas.soares"
        st.session_state["identidades_azure_cache"] = {"admin": "vinicius"}
        self.assertEqual(_Falso()._tag_criado_por(), "criado-por:jonatas.soares")


if __name__ == "__main__":
    unittest.main()
