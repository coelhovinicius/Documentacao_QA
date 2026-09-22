"""Texto corrido dos PDFs sai justificado; título, cabeçalho e código não."""
import unittest

from reportlab.lib.enums import TA_JUSTIFY

from qa_testgen.infrastructure.manual_pdf import ManualPdfGenerator
from qa_testgen.infrastructure.pdf_report import PdfReportGenerator


class PdfAlinhamentoTests(unittest.TestCase):
    def test_relatorio_justifica_corpo_e_celula(self):
        estilos = PdfReportGenerator._styles()
        self.assertEqual(estilos['body'].alignment, TA_JUSTIFY)
        self.assertEqual(estilos['cell'].alignment, TA_JUSTIFY)
        for nao_justificar in ('title', 'section', 'subsection', 'cell_head'):
            self.assertNotEqual(estilos[nao_justificar].alignment, TA_JUSTIFY, nao_justificar)

    def test_manual_justifica_introducao_passo_e_aviso(self):
        estilos = ManualPdfGenerator._styles()
        for justificado in ('intro', 'passo_desc', 'aviso'):
            self.assertEqual(estilos[justificado].alignment, TA_JUSTIFY, justificado)
        for nao_justificar in ('title', 'passo_titulo', 'img_caption'):
            self.assertNotEqual(estilos[nao_justificar].alignment, TA_JUSTIFY, nao_justificar)

    def test_pdfs_continuam_sendo_gerados(self):
        texto = ("Este parágrafo é longo o bastante para quebrar em várias linhas e, assim, "
                 "mostrar a diferença entre texto alinhado à esquerda e texto justificado. " * 3)
        pdf_manual = ManualPdfGenerator.generate(
            titulo="Manual de Teste", introducao=texto,
            passos=[{"numero": 1, "titulo": "Abrir a tela", "descricao": texto, "aviso": texto}],
            passo_images={}, img_by_filename={}, author_name="QA",
        )
        self.assertTrue(pdf_manual.startswith(b"%PDF"))
        pdf_relatorio = PdfReportGenerator.generate(
            project_name="Projeto", matriz=[{"id": "MC-001", "funcionalidade": "Login", "requisito": "RF-01",
                                             "cenario": texto, "categoria": "Fluxo Principal", "prioridade": "Alta",
                                             "criticidade": "Alta", "observacoes": "-"}],
            test_plans=[], test_cases=[{"titulo": "Caso", "pre_condicoes": texto,
                                        "passos": [{"numero": 1, "acao": texto, "resultado_esperado": texto}]}],
            author_name="QA",
        )
        self.assertTrue(pdf_relatorio.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
