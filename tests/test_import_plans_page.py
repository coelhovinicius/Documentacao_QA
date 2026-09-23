"""Tela 'Subir CSV de Planos': ler o arquivo, conferir e entregar tudo ao Passo 7."""
import unittest

import qa_testgen.ui.application as app
from qa_testgen.infrastructure.csv_formatter import AzureCsvFormatter
from qa_testgen.ui.application import UserInterface

CASOS = [
    {"titulo": "Login com credenciais válidas",
     "pre_condicoes": "Usuário cadastrado e ativo.",
     "requisitos_relacionados": ["MC-001 HML"],
     "passos": [{"numero": 1, "acao": "Informar e-mail e senha.", "resultado_esperado": "Acesso liberado."}]},
]
PLANOS = [{"nome": "Plano de Teste – Acesso", "suites": [{"nome": "Login", "casos": [CASOS[0]["titulo"]]}]}]


class _Rerun(Exception):
    """st.rerun() interrompe a renderização — aqui vira exceção pra parar o teste no mesmo ponto."""


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Arquivo:
    def __init__(self, nome, conteudo):
        self.name = nome
        self._conteudo = conteudo

    def getvalue(self):
        return self._conteudo


class _FakeSt:
    """Só o suficiente do streamlit pra rodar a página sem navegador."""

    def __init__(self, clicar=(), upload=None, texto=None):
        self.clicar = set(clicar)
        self.upload = upload
        self.texto = texto
        self.botoes = []
        self.mensagens = []

    def button(self, label, **kw):
        self.botoes.append((kw.get("key"), label))
        return kw.get("key") in self.clicar

    def file_uploader(self, _label, **_kw):
        return self.upload

    def text_input(self, _label, value="", **_kw):
        return self.texto if self.texto is not None else value

    def columns(self, spec, **_kw):
        quantidade = spec if isinstance(spec, int) else len(spec)
        return [_Ctx() for _ in range(quantidade)]

    def expander(self, *_a, **_kw):
        return _Ctx()

    def container(self, *_a, **_kw):
        return _Ctx()

    def error(self, texto, **_kw):
        self.mensagens.append(("error", texto))

    def warning(self, texto, **_kw):
        self.mensagens.append(("warning", texto))

    def info(self, texto, **_kw):
        self.mensagens.append(("info", texto))

    def success(self, texto, **_kw):
        self.mensagens.append(("success", texto))

    def caption(self, texto, **_kw):
        self.mensagens.append(("caption", texto))

    def markdown(self, texto, **_kw):
        self.mensagens.append(("markdown", texto))

    def subheader(self, texto, **_kw):
        self.mensagens.append(("subheader", texto))

    def divider(self):
        pass

    def rerun(self):
        raise _Rerun()

    def textos(self, tipo=None):
        return [t for k, t in self.mensagens if tipo is None or k == tipo]


class _Estado:
    def __init__(self, **inicial):
        self.dados = dict(inicial)

    def get(self, chave, default=None):
        valor = self.dados.get(chave, default)
        return default if valor is None and default is not None else valor

    def set(self, chave, valor):
        self.dados[chave] = valor


class _Tela:
    """A página e o rodapé do Passo 7, sem subir o app inteiro."""

    _import_plans_page = UserInterface._import_plans_page
    _import_plans_limpar = UserInterface._import_plans_limpar
    _render_step7_back_and_new = UserInterface._render_step7_back_and_new
    _set_step = UserInterface._set_step

    def __init__(self, permissao=True, **estado):
        self.state = _Estado(**estado)
        self._permissao = permissao
        self.passo_7_chamado = 0
        self.logs = []

    def _get_permission_cached(self, _permissao):
        return self._permissao

    def _log(self, *args):
        self.logs.append(args)

    def step_7(self):
        self.passo_7_chamado += 1


def _csv(ambiente="Homologação", projeto="Projeto X"):
    return AzureCsvFormatter.plans_suites_cases(PLANOS, CASOS, projeto, ambiente).encode("utf-8")


class PaginaTests(unittest.TestCase):
    def setUp(self):
        self.original_st = app.st
        self.addCleanup(lambda: setattr(app, "st", self.original_st))

    def _render(self, tela, **kwargs):
        app.st = _FakeSt(**kwargs)
        try:
            tela._import_plans_page()
        except _Rerun:
            pass
        return app.st

    def test_sem_permissao_nao_le_arquivo_nenhum(self):
        tela = _Tela(permissao=False)
        fake = self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()))
        self.assertIn("não tem permissão", " ".join(fake.textos("error")))
        self.assertIsNone(tela.state.get("import_plans_resultado"))

    def test_sem_arquivo_apenas_pede_o_arquivo(self):
        fake = self._render(_Tela())
        self.assertTrue(any("Escolha o arquivo" in t for t in fake.textos("info")))

    def test_arquivo_errado_mostra_o_motivo_e_nao_avanca(self):
        tela = _Tela()
        fake = self._render(tela, upload=_Arquivo("lista.csv", b"Nome,Email\nJoao,j@x.com\n"))
        self.assertTrue(any("não parece o CSV de Planos" in t for t in fake.textos("error")))
        self.assertFalse(tela.state.get("import_plans_integrar"))
        self.assertIsNone(tela.state.get("test_cases"))

    def test_resumo_e_aviso_de_vinculo_aparecem_antes_de_integrar(self):
        fake = self._render(_Tela(), upload=_Arquivo("QA_Plans.csv", _csv()))
        self.assertTrue(any("1 plano(s)" in t and "1 caso(s)" in t for t in fake.textos("success")))
        self.assertTrue(any("não traz o número do Work Item" in t for t in fake.textos("info")))

    def test_ler_o_arquivo_nao_mexe_no_azure_nem_no_estado_do_envio(self):
        tela = _Tela()
        self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()))
        self.assertIsNone(tela.state.get("test_cases"))
        self.assertIsNone(tela.state.get("test_plans"))
        self.assertEqual(tela.passo_7_chamado, 0)

    def test_botao_leva_o_conteudo_do_csv_pro_passo_7(self):
        tela = _Tela()
        self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()), clicar={"btn_import_plans_go"})
        self.assertEqual([c["titulo"] for c in tela.state.get("test_cases")], [CASOS[0]["titulo"]])
        self.assertEqual(tela.state.get("test_plans")[0]["nome"], PLANOS[0]["nome"])
        self.assertEqual(tela.state.get("ambiente_testes"), "Homologação")
        self.assertEqual(tela.state.get("project_name"), "Projeto X")
        self.assertTrue(tela.state.get("import_plans_integrar"))
        self.assertEqual(len(tela.logs), 1)

    def test_envio_anterior_nao_contamina_o_csv_novo(self):
        tela = _Tela(ado_test_case_ids={"velho": 1}, ado_wi_case_links={"9501": ["velho"]},
                     ado_excluded_case_titles=["velho"], ado_full_push_log=["log velho"])
        self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()), clicar={"btn_import_plans_go"})
        for chave in ("ado_test_case_ids", "ado_wi_case_links", "ado_excluded_case_titles", "ado_full_push_log"):
            self.assertIsNone(tela.state.get(chave), chave)

    def test_avisa_que_os_casos_do_assistente_serao_substituidos(self):
        tela = _Tela(test_cases=[{"titulo": "Caso gerado no Passo 5", "passos": []}])
        fake = self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()))
        self.assertTrue(any("já tem 1 Caso(s) de Teste do assistente" in t for t in fake.textos("warning")))

    def test_nao_avisa_substituicao_quando_os_casos_sao_os_do_proprio_arquivo(self):
        tela = _Tela()
        self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()), clicar={"btn_import_plans_go"})
        tela.state.set("import_plans_integrar", False)   # voltou pra tela de upload
        fake = self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()))
        self.assertFalse([t for t in fake.textos("warning") if "assistente" in t])

    def test_nome_do_projeto_editado_na_tela_vence_o_do_arquivo(self):
        tela = _Tela()
        self._render(tela, upload=_Arquivo("QA_Plans.csv", _csv()), texto="  Nova 360  ",
                     clicar={"btn_import_plans_go"})
        self.assertEqual(tela.state.get("project_name"), "Nova 360")

    def test_segunda_fase_renderiza_o_passo_7_e_nao_o_uploader(self):
        resultado = {"test_cases": CASOS, "test_plans": PLANOS, "ambiente": "Homologação",
                     "projeto": "Projeto X", "avisos": [], "erro": ""}
        tela = _Tela(import_plans_resultado=resultado, import_plans_integrar=True,
                     import_plans_arquivo="QA_Plans.csv")
        fake = self._render(tela)
        self.assertEqual(tela.passo_7_chamado, 1)
        self.assertTrue(any("QA_Plans.csv" in t for t in fake.textos("caption")))

    def test_trocar_arquivo_volta_pra_tela_de_upload(self):
        resultado = {"test_cases": CASOS, "test_plans": PLANOS, "ambiente": "", "projeto": "",
                     "avisos": [], "erro": ""}
        tela = _Tela(import_plans_resultado=resultado, import_plans_integrar=True,
                     import_plans_arquivo="QA_Plans.csv")
        self._render(tela, clicar={"btn_import_plans_trocar"})
        self.assertFalse(tela.state.get("import_plans_integrar"))
        self.assertEqual(tela.passo_7_chamado, 0)


class LimparTests(unittest.TestCase):
    def test_limpar_apaga_o_que_veio_do_arquivo_e_troca_a_chave_do_uploader(self):
        tela = _Tela(import_plans_resultado={"x": 1}, import_plans_integrar=True,
                     import_plans_arquivo="QA_Plans.csv", test_cases=CASOS, test_plans=PLANOS,
                     ado_board_items=[{"id": 1}], import_plans_versao=2)
        tela._import_plans_limpar()
        for chave in ("import_plans_resultado", "import_plans_integrar", "import_plans_arquivo",
                      "test_cases", "test_plans", "ado_board_items"):
            self.assertIsNone(tela.state.get(chave), chave)
        # a chave do file_uploader muda junto — sem isso o Streamlit continuaria
        # exibindo o arquivo anterior já "escolhido"
        self.assertEqual(tela.state.get("import_plans_versao"), 3)


class RodapeDoPasso7Tests(unittest.TestCase):
    def setUp(self):
        self.original_st = app.st
        self.addCleanup(lambda: setattr(app, "st", self.original_st))

    def _render(self, tela, clicar=()):
        app.st = _FakeSt(clicar=clicar)
        try:
            tela._render_step7_back_and_new("main")
        except _Rerun:
            pass
        return app.st

    def _tela_csv(self):
        return _Tela(show_import_plans_page=True, import_plans_integrar=True, step=1,
                     import_plans_resultado={"test_cases": CASOS})

    def test_vindo_do_csv_voltar_e_trocar_de_arquivo_e_nao_ir_pro_passo_6(self):
        tela = self._tela_csv()
        self._render(tela, clicar={"btn_back_step7_main"})
        self.assertFalse(tela.state.get("import_plans_integrar"))
        self.assertEqual(tela.state.get("step"), 1)   # continua fora do assistente

    def test_vindo_do_csv_o_botao_primario_vira_outro_csv(self):
        fake = self._render(self._tela_csv())
        self.assertIn("🆕 Outro CSV", [rotulo for _chave, rotulo in fake.botoes])

    def test_outro_csv_zera_o_arquivo_anterior(self):
        tela = self._tela_csv()
        self._render(tela, clicar={"btn_new_step7_main"})
        self.assertIsNone(tela.state.get("import_plans_resultado"))
        self.assertFalse(tela.state.get("show_new_analysis_modal"))

    def test_no_assistente_continua_voltando_pro_passo_6(self):
        tela = _Tela(step=7, max_step=7)
        self._render(tela, clicar={"btn_back_step7_main"})
        self.assertEqual(tela.state.get("step"), 6)

    def test_no_assistente_o_botao_primario_continua_sendo_nova_analise(self):
        tela = _Tela(step=7)
        fake = self._render(tela, clicar={"btn_new_step7_main"})
        self.assertIn("🔄 Nova Análise", [rotulo for _chave, rotulo in fake.botoes])
        self.assertTrue(tela.state.get("show_new_analysis_modal"))


if __name__ == "__main__":
    unittest.main()
