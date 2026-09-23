"""Importar de volta o CSV QA_Plans_*.csv que o próprio app exporta."""
import unittest

from qa_testgen.infrastructure.csv_formatter import AzureCsvFormatter
from qa_testgen.infrastructure.csv_importer import importar_planos_csv, resumo_importacao

CASOS = [
    {"titulo": "Verificação da Integração Inicial de Dados de Cobrança (Happy Path)",
     "pre_condicoes": "Dado que a integração está ativa e há fatura em aberto.",
     "requisitos_relacionados": ["MC-001 HML"],
     "passos": [{"numero": 1, "acao": "Confirmar a carga inicial.", "resultado_esperado": "Dados enviados ao HubSpot."},
                {"numero": 2, "acao": "Localizar o contato pelo CPF.", "resultado_esperado": "Contato encontrado."}]},
    {"titulo": "Atualização de Status de Pagamento para 'Cancelado'",
     "pre_condicoes": "Dado que existe fatura 'Pendente', alterada para 'Cancelado'.",
     "requisitos_relacionados": ["MC-007 HML"],
     "passos": [{"numero": 1, "acao": "Alterar status no BI/Lake.", "resultado_esperado": "Status 'Cancelado'."}]},
]
PLANOS = [
    {"nome": "Plano de Teste – Integração BI → HubSpot", "suites": [
        {"nome": "Integração de Dados (Happy Path)", "casos": [CASOS[0]["titulo"]]}]},
    {"nome": "Plano de Teste – Fluxo de Negócio", "suites": [
        {"nome": "Manutenção de Status", "casos": [CASOS[1]["titulo"]]}]},
]


def _csv(ambiente="Homologação"):
    return AzureCsvFormatter.plans_suites_cases(PLANOS, CASOS, "Projeto X", ambiente).encode("utf-8")


class IdaEVoltaTests(unittest.TestCase):
    def setUp(self):
        self.r = importar_planos_csv("QA_Plans_projeto.csv", _csv())

    def test_sem_erro_e_com_resumo(self):
        self.assertEqual(self.r["erro"], "")
        self.assertEqual(self.r["avisos"], [])
        self.assertEqual(resumo_importacao(self.r), "2 plano(s) · 2 suíte(s) · 2 caso(s) · 3 passo(s)")

    def test_casos_voltam_identicos(self):
        self.assertEqual([c["titulo"] for c in self.r["test_cases"]], [c["titulo"] for c in CASOS])
        self.assertEqual([c["pre_condicoes"] for c in self.r["test_cases"]], [c["pre_condicoes"] for c in CASOS])
        self.assertEqual([c["requisitos_relacionados"] for c in self.r["test_cases"]],
                         [c["requisitos_relacionados"] for c in CASOS])

    def test_passos_na_ordem_com_acao_e_esperado(self):
        passos = self.r["test_cases"][0]["passos"]
        self.assertEqual([p["numero"] for p in passos], [1, 2])
        self.assertEqual(passos[0]["acao"], "Confirmar a carga inicial.")
        self.assertEqual(passos[1]["resultado_esperado"], "Contato encontrado.")

    def test_prefixo_ct_nao_volta_no_titulo(self):
        # o exportador escreve "CT01 HML - <titulo>"; se o prefixo voltasse, o envio
        # ao Azure criaria "CT01 HML - CT01 HML - <titulo>"
        for caso in self.r["test_cases"]:
            self.assertNotRegex(caso["titulo"], r"^CT\s*\d+")

    def test_ambiente_vem_do_prefixo(self):
        self.assertEqual(self.r["ambiente"], "Homologação")
        self.assertEqual(importar_planos_csv("x.csv", _csv("Produção"))["ambiente"], "Produção")
        self.assertEqual(importar_planos_csv("x.csv", _csv(""))["ambiente"], "")

    def test_projeto_vem_da_coluna_area_path(self):
        # o exportador grava o nome do projeto na coluna "Area Path" -- e ele
        # volta como sugestao do nome do Test Plan no envio
        self.assertEqual(self.r["projeto"], "Projeto X")

    def test_planos_e_suites_preservam_ordem_e_composicao(self):
        planos = self.r["test_plans"]
        self.assertEqual([p["nome"] for p in planos], [p["nome"] for p in PLANOS])
        self.assertEqual(planos[0]["suites"][0]["casos"], [CASOS[0]["titulo"]])
        self.assertEqual(planos[1]["suites"][0]["nome"], "Manutenção de Status")


class ArquivoRuimTests(unittest.TestCase):
    def test_csv_de_outra_coisa_e_recusado_com_motivo(self):
        r = importar_planos_csv("lista.csv", b"Nome,Email\nJoao,j@x.com\n")
        self.assertIn("não parece o CSV de Planos", r["erro"])
        self.assertIn("Title", r["erro"])

    def test_arquivo_vazio(self):
        self.assertIn("vazio", importar_planos_csv("x.csv", b"ID,Title,Step Action\n")["erro"])

    def test_passo_orfao_vira_aviso_e_nao_derruba(self):
        csv = ("ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected,Suite,Plan\n"
               ",,,1,,Passo perdido,Resultado,S,P\n"
               ",Test Case,CT01 HML - Caso bom,,Pré,,,S,P\n"
               ",,,1,,Fazer algo,Deu certo,S,P\n").encode("utf-8")
        r = importar_planos_csv("x.csv", csv)
        self.assertEqual(r["erro"], "")
        self.assertEqual(len(r["test_cases"]), 1)
        self.assertEqual(len(r["test_cases"][0]["passos"]), 1)
        self.assertTrue(any("sem nenhum caso de teste antes" in a for a in r["avisos"]))

    def test_caso_sem_passos_e_titulo_repetido_viram_aviso(self):
        csv = ("ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected,Suite,Plan\n"
               ",Test Case,CT01 HML - Caso sem passo,,Pré,,,S,P\n"
               ",Test Case,CT02 HML - Caso sem passo,,Pré,,,S,P\n").encode("utf-8")
        r = importar_planos_csv("x.csv", csv)
        self.assertTrue(any("sem nenhum passo" in a for a in r["avisos"]))
        self.assertTrue(any("repetido" in a for a in r["avisos"]))

    def test_coluna_opcional_de_work_item_e_aproveitada(self):
        csv = ("ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected,Suite,Plan,Work Item\n"
               ",Test Case,CT01 HML - Caso,,Pré,,,S,P,8351\n"
               ",,,1,,Fazer,Deu certo,S,P,\n").encode("utf-8")
        r = importar_planos_csv("x.csv", csv)
        self.assertEqual(r["test_cases"][0]["work_item_relacionado"], "8351")
        self.assertIn("1 já com Work Item indicado", resumo_importacao(r))

    def test_sem_area_path_o_projeto_fica_vazio(self):
        csv = ("ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected\n"
               ",Test Case,Caso solto,,Pré,,\n"
               ",,,1,,Fazer,Deu certo\n").encode("utf-8")
        self.assertEqual(importar_planos_csv("x.csv", csv)["projeto"], "")

    def test_sem_suite_ou_plano_usa_nome_padrao(self):
        csv = ("ID,Work Item Type,Title,Test Step,Pre condicoes,Step Action,Step Expected\n"
               ",Test Case,Caso solto,,Pré,,\n"
               ",,,1,,Fazer,Deu certo\n").encode("utf-8")
        r = importar_planos_csv("x.csv", csv)
        self.assertEqual(r["test_plans"][0]["nome"], "Plano de Teste")
        self.assertEqual(r["test_plans"][0]["suites"][0]["nome"], "Casos de Teste")


if __name__ == "__main__":
    unittest.main()
