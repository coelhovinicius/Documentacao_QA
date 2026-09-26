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

    def test_caracteres_sem_glifo_viram_equivalentes(self):
        # "1‑5" com hífen U+2011 (comum em texto de IA) saía "15"; "≤ 500 ms" saía "500 ms"
        self.assertEqual(PdfReportGenerator._esc("Likert 1‑5"), "Likert 1-5")
        self.assertEqual(PdfReportGenerator._esc("tempo ≤ 500 ms"), "tempo &lt;= 500 ms")
        self.assertEqual(PdfReportGenerator._esc("a → b"), "a -&gt; b")

    def test_quebras_de_linha_das_pre_condicoes_sao_mantidas(self):
        self.assertEqual(PdfReportGenerator._esc_linhas("Base URL: x\nAmbiente: HML\n"), "Base URL: x<br/>Ambiente: HML")
        self.assertEqual(PdfReportGenerator._esc_linhas("<b>"), "&lt;b&gt;")

    def test_quebra_de_pagina_nao_deixa_pagina_em_branco(self):
        from reportlab.platypus import PageBreak, Paragraph, Spacer
        story = [Paragraph("fim da seção", PdfReportGenerator._styles()['body']), Spacer(1, 8), Spacer(1, 14)]
        PdfReportGenerator._nova_pagina(story)
        self.assertIsInstance(story[-1], PageBreak)
        self.assertIsInstance(story[-2], Paragraph)   # os Spacers do fim saíram

    def test_resposta_enorme_nao_derruba_o_pdf_de_testes_de_api(self):
        # HTTP 500 com o trace inteiro: uma linha gigante + muitas linhas — antes virava uma célula única
        # mais alta que a página e o ReportLab abortava o PDF ("Flowable ... too large on page")
        from qa_testgen.domain.models.api_test import ApiAssertionResult, ApiCaseResult
        from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder as E
        corpo = '{"message": "Server Error", "trace": [' + ", ".join(
            f'{{"file": "/var/www/app/vendor/pacote/Arquivo{i}.php", "line": {i}, "function": "handle"}}' for i in range(400)
        ) + "]}\n" + "\n".join(f"#{i} /var/www/app/Http/Kernel.php({i}): handle()" for i in range(300))
        r = ApiCaseResult(case_id="a", nome="Caso com 500", metodo="GET", url_final="https://api.x/y", request_headers={},
                          request_body="", status_code=500, status_text="Server Error", response_headers={},
                          response_body=corpo, tempo_ms=170, assercoes=[ApiAssertionResult("Status 200", False, "obtido 500")])
        textos = {"a": {"request": E.texto_request(r, []), "response": E.texto_response(r, [])}}
        pdf = PdfReportGenerator.generate_api_test_report("P", "Homologação", "https://api.x", [r], E.resumo([r]), textos,
                                                          author_name="QA")
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_bloco_de_codigo_quebra_linha_longa_e_divide_em_partes(self):
        from reportlab.lib.units import cm
        t = PdfReportGenerator._bloco_codigo(PdfReportGenerator._styles(), "x" * 5000, 17 * cm, max_linhas=60)
        self.assertGreater(len(t._cellvalues), 1)          # várias linhas de tabela (pode continuar na outra página)

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
