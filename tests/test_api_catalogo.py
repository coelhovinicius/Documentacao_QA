"""Catálogo de rotas reais (api_discovery): extração do bundle/OpenAPI/Postman/texto, normalização, casamento e verificação."""
import unittest

from qa_testgen.infrastructure import api_discovery as d
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner
from qa_testgen.domain.models.api_test import ApiTestCase, ApiAssertion

BUNDLE = """var Zl=`/api`;async function a(){return K.get(`/v1/me`)}async function b(e){return K.post(`/v1/psychosocial-surveys/${e}/responses`,{consent:t})}
K.patch(`/v1/psychosocial-surveys/${e}/close`);K.download(`/v1/action-plans/export`);x.get("/v1/psychosocial-surveys/current");
fetch({method:"DELETE",url:`/v1/psychosocial-surveys/${e}`});var y=`/api/v1/literal/path`;"""
HTML = '<html><head><script type="module" crossorigin src="/assets/index-AB12.js"></script><link rel="modulepreload" href="/assets/vendor.js"></head></html>'


class ExtracaoTests(unittest.TestCase):
    def test_bundle_routes_with_prefix_and_id_placeholders(self):
        rotas = d.extrair_rotas_de_bundle(BUNDLE)
        self.assertIn(("GET", "/api/v1/me"), rotas)
        self.assertIn(("POST", "/api/v1/psychosocial-surveys/{id}/responses"), rotas)
        self.assertIn(("PATCH", "/api/v1/psychosocial-surveys/{id}/close"), rotas)
        self.assertIn(("GET", "/api/v1/action-plans/export"), rotas)          # download -> GET
        self.assertIn(("GET", "/api/v1/psychosocial-surveys/current"), rotas)
        self.assertIn(("DELETE", "/api/v1/psychosocial-surveys/{id}"), rotas)
        self.assertIn(("GET", "/api/v1/literal/path"), rotas)
        self.assertEqual(d.descobrir_bundles(HTML, "https://h.com/"), ["https://h.com/assets/index-AB12.js", "https://h.com/assets/vendor.js"])

    def test_bundle_routes_without_api_v1_prefix(self):
        # Front do passaporte: chamadas explícitas em /api-candidate, /core/sso, /bff — nada de /api/v1.
        js = """h.get(`/api-candidate/candidate/id`);h.post("/core/sso/api/v1/account/forgot-password",e);
        h.delete(`/api-candidate/candidateacademicformation/${e}`);h.get(`/bff/iped/course-access?courseId=${encodeURIComponent(e)}`);
        h.get(`/assets/logo.png`);h.get("/");h.get(`/index.html`);h.get(`/login`);"""
        rotas = d.extrair_rotas_de_bundle(js)
        self.assertIn(("GET", "/api-candidate/candidate/id"), rotas)
        self.assertIn(("POST", "/core/sso/api/v1/account/forgot-password"), rotas)
        self.assertIn(("DELETE", "/api-candidate/candidateacademicformation/{id}"), rotas)
        self.assertIn(("GET", "/bff/iped/course-access"), rotas)               # query string cai fora
        for fora in ("/assets/logo.png", "/", "/index.html", "/login"):         # asset, raiz, página, 1 segmento
            self.assertNotIn(("GET", fora), rotas)

    def test_openapi_postman_and_text(self):
        openapi = {"basePath": "/api/v1", "paths": {"/users/{userId}": {"get": {}, "delete": {}}, "/login": {"post": {}}}}
        self.assertEqual(d.extrair_rotas_de_openapi(openapi), [("DELETE", "/api/v1/users/{id}"), ("GET", "/api/v1/users/{id}"), ("POST", "/api/v1/login")])
        postman = {"item": [{"name": "p", "item": [{"request": {"method": "GET", "url": {"raw": "{{base_url}}/api/v1/me?x=1"}}}]},
                            {"request": {"method": "POST", "url": "https://h.com/api/v1/auth/login"}}]}
        self.assertEqual(d.extrair_rotas_de_postman(postman), [("GET", "/api/v1/me"), ("POST", "/api/v1/auth/login")])
        self.assertEqual(d.extrair_rotas_de_texto("GET /api/v1/me\n- POST /api/v1/auth/login\n/api/v1/x/{id}\n\n"),
                         [("GET", "/api/v1/me"), ("GET", "/api/v1/x/{id}"), ("POST", "/api/v1/auth/login")])

    def test_normalizacao(self):
        base = "https://360.hml.refuturiza.com.br"
        self.assertEqual(d.normalizar_caminho("{{base_url}}/api/v1/surveys/{{survey_id}}/questions/", base), "/api/v1/surveys/{id}/questions")
        self.assertEqual(d.normalizar_caminho(base + "/api/v1/items/42?x=1", base), "/api/v1/items/{id}")
        self.assertEqual(d.normalizar_caminho("/api/v1/a/0f1e2d3c-1111-2222-3333-444455556666/b"), "/api/v1/a/{id}/b")
        self.assertEqual(d.normalizar_caminho("{{base_url}}/api/v1/surveys//questions", base), "/api/v1/surveys/questions")


class CasamentoEVerificacaoTests(unittest.TestCase):
    CAT = [{"metodo": "GET", "caminho": "/api/v1/psychosocial-surveys/{id}/heatmap"}, {"metodo": "POST", "caminho": "/api/v1/auth/login"}]

    def test_casar_com_catalogo(self):
        base = "https://h.com"
        self.assertTrue(d.casar_com_catalogo("GET", "{{base_url}}/api/v1/psychosocial-surveys/{{survey_id}}/heatmap", self.CAT, base))
        self.assertTrue(d.casar_com_catalogo("get", "https://h.com/api/v1/psychosocial-surveys/7/heatmap?d=1", self.CAT, base))
        self.assertFalse(d.casar_com_catalogo("GET", "{{base_url}}/api/v1/surveys", self.CAT, base))          # rota inventada
        self.assertFalse(d.casar_com_catalogo("GET", "{{base_url}}/api/v1/auth/login", self.CAT, base))       # método errado
        self.assertFalse(d.casar_com_catalogo("GET", "{{base_url}}/api/v1/psychosocial-surveys/{{id}}/heatmap/x", self.CAT, base))

    def test_rota_nao_encontrada(self):
        self.assertTrue(d.rota_nao_encontrada(404, '{"message": "The route api/v1/surveys could not be found."}'))
        self.assertTrue(d.rota_nao_encontrada(404, "<html>nope</html>"))
        self.assertFalse(d.rota_nao_encontrada(404, '{"message": "No query results for model"}'))   # recurso, não rota
        self.assertFalse(d.rota_nao_encontrada(401, '{"message": "Unauthenticated."}'))

    def test_sondas_para_casos_e_veredito(self):
        casos = [{"metodo": "GET", "url": "{{base_url}}/api/v1/s/{{sid}}/q"}, {"metodo": "get", "url": "{{base_url}}/api/v1/s/{{sid}}/q"},
                 {"metodo": "POST", "url": "{{base_url}}/api/v1/auth/login"}]
        sondas = d.montar_sondas_para_casos(casos, "https://h.com/")
        self.assertEqual([s["url"] for s in sondas], ["https://h.com/api/v1/s/1/q", "https://h.com/api/v1/auth/login"])
        veredito = d.verificar_respostas_das_sondas(sondas, [
            {"status": 404, "body": '{"message": "The route api/v1/s/1/q could not be found."}'},
            {"status": 422, "body": '{"message": "api.errors.validation_failed"}'}])
        self.assertEqual(veredito, {("GET", "/api/v1/s/{id}/q"): False, ("POST", "/api/v1/auth/login"): True})

    def test_prompt_block_limits_and_marks_rule(self):
        cat = [{"metodo": "GET", "caminho": f"/api/v1/r{i}"} for i in range(80)] + [{"metodo": "GET", "caminho": "/api/v1/psychosocial-dimensions"}]
        bloco = d.rotas_para_prompt(cat, "perguntas psychosocial dimensions")
        self.assertIn("SOMENTE", bloco)
        self.assertLessEqual(bloco.count("\n- "), d._MAX_ROTAS_NO_PROMPT)
        self.assertIn("- GET /api/v1/psychosocial-dimensions", bloco.split("\n")[1])   # a mais parecida vem primeiro
        self.assertEqual(d.rotas_para_prompt([], "x"), "")

    def test_relevance_is_accent_insensitive_and_generic(self):
        # Card em português acentuado x rotas em inglês (ou português sem acento) de um front qualquer.
        cat = [{"metodo": "POST", "caminho": "/api-candidate/candidate/importar-curriculo/arquivo"},
               {"metodo": "POST", "caminho": "/api-central/portal/candidatar"},
               {"metodo": "GET", "caminho": "/bff/iped/certificates"},
               {"metodo": "POST", "caminho": "/bff/auth/login"},
               {"metodo": "GET", "caminho": "/api-central/portal/vaga/{id}"}]
        rel = d.rotas_relevantes(cat, "Candidatura à vaga com currículo importado", limite=4)
        caminhos = [r["caminho"] for r in rel]
        self.assertEqual(caminhos[0], "/bff/auth/login")                                   # login sempre primeiro
        self.assertIn("/api-candidate/candidate/importar-curriculo/arquivo", caminhos)     # currículo -> curriculo
        self.assertIn("/api-central/portal/candidatar", caminhos)                          # candidatura -> candidatar
        self.assertIn("/api-central/portal/vaga/{id}", caminhos)
        self.assertNotIn("/bff/iped/certificates", caminhos)                               # irrelevante fica de fora

    def test_relevance_downweights_domain_wide_words_and_reads_glued_names(self):
        # "candidato" bate em quase tudo num portal de candidatos -> pesa pouco; "experiência
        # profissional" tem que achar candidateprofessionalexperience (nome colado, em inglês).
        cat = [{"metodo": "POST", "caminho": c} for c in (
            "/api-candidate/candidateprofessionalexperience", "/api-candidate/candidateacademicformation",
            "/api-central/candidato/retomarcandidatura", "/api-candidate/jobcandidate/descandidatar",
            "/api-candidate/candidate/id", "/api-candidate/candidate/update", "/api-avaliacao/disctest/candidate/{id}")]
        rel = d.rotas_relevantes(cat, "Cadastro de experiência profissional e formação acadêmica no perfil do candidato.", limite=2)
        self.assertEqual([r["caminho"] for r in rel],
                         ["/api-candidate/candidateprofessionalexperience", "/api-candidate/candidateacademicformation"])

    def test_probes_use_catalog_when_card_cites_no_route(self):
        cat = [{"metodo": "POST", "caminho": "/bff/auth/login"}, {"metodo": "GET", "caminho": "/api-central/portal/vaga/{id}"},
               {"metodo": "POST", "caminho": "/api-central/portal/candidatar"}]
        sondas = d.montar_sondas("Candidatar-se a uma vaga", "https://h.com", cat)
        urls = [s["url"] for s in sondas]
        self.assertIn("https://h.com/bff/auth/login", urls)
        self.assertIn("https://h.com/api-central/portal/vaga/1", urls)             # {id} vira 1
        self.assertFalse(any("/api/v1" in u for u in urls))                       # rota real: sem variante inventada
        # sem catálogo, continua sondando as convencionais
        self.assertIn("https://h.com/api/v1/auth/login", [s["url"] for s in d.montar_sondas("texto sem rota", "https://h.com")])
        # rota citada no card prevalece sobre o catálogo
        self.assertIn("https://h.com/x/y", [s["url"] for s in d.montar_sondas("GET /x/y", "https://h.com", cat)])


class RunnerRotaInexistenteTests(unittest.TestCase):
    def test_404_route_not_found_becomes_definition_error(self):
        runner = ApiTestRunner({"base_url": "http://x"})

        class _Resp:
            status_code, reason, headers, text = 404, "Not Found", {"Content-Type": "application/json"}, '{"message": "The route api/v1/nada could not be found."}'

            def json(self):
                return {"message": "The route api/v1/nada could not be found."}

        caso = ApiTestCase(id="1", nome="x", metodo="GET", url="http://x/api/v1/nada", assercoes=[ApiAssertion(tipo="status", valor="200")])
        from qa_testgen.domain.models.api_test import ApiCaseResult
        res = ApiCaseResult(case_id="1", nome="x", metodo="GET", url_final="http://x/api/v1/nada", request_headers={}, request_body="",
                            status_code=None, status_text="", response_headers={}, response_body="", tempo_ms=1)
        runner._concluir_resultado(caso, res, _Resp())
        self.assertTrue(res.erro.startswith("Rota inexistente"))
        self.assertEqual(res.resultado_label, "Erro")


if __name__ == "__main__":
    unittest.main()
