import json
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from qa_testgen.domain.models.api_test import ApiAssertion, ApiTestCase
from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder, MASCARA
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner
from qa_testgen.infrastructure.postman_importer import PostmanImporter, PostmanImportError


COLLECTION = {
    "info": {"name": "Demo", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "variable": [{"key": "base_url", "value": "http://x"}, {"key": "valid_password", "value": "s3cr3t"}],
    "item": [
        {
            "name": "Login",
            "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": [
                "pm.test('Status 200', () => pm.response.to.have.status(200));",
                "const body = pm.response.json();",
                "pm.test('token', () => { pm.expect(body.data.token, 'tok').to.be.a('string').and.not.empty; });",
                "pm.test('tt', () => pm.expect(body.data.token_type).to.eql('Bearer'));",
                "pm.test('no errors', () => pm.expect(body).to.not.have.property('errors'));",
                "pm.collectionVariables.set('auth_token', body.data.token);",
            ]}}],
            "request": {
                "method": "POST",
                "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": {"mode": "raw", "raw": "{\"email\": \"a@b.c\", \"password\": \"{{valid_password}}\"}"},
                "url": {"raw": "{{base_url}}/login", "host": ["{{base_url}}"], "path": ["login"]},
            },
        },
        {
            "name": "Pasta",
            "item": [{
                "name": "Me",
                "event": [{"listen": "test", "script": {"exec": [
                    "pm.test('ok', () => pm.response.to.have.status(200));",
                    "const d = pm.response.json().data || {};",
                    "pm.test('roles', () => { pm.expect(d.roles.tenant).to.be.an('array'); });",
                    "[...d.roles.tenant].forEach(r => { pm.expect(r).to.have.property('name'); });",
                ]}}],
                "request": {"method": "GET", "header": [{"key": "Authorization", "value": "Bearer {{auth_token}}"}],
                            "url": "{{base_url}}/me"},
            }],
        },
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("password") == "s3cr3t":
            self._send(200, {"data": {"token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop", "token_type": "Bearer"}})
        else:
            self._send(401, {"message": "invalid"})

    def do_GET(self):
        if self.headers.get("Authorization", "").startswith("Bearer eyJ"):
            self._send(200, {"data": {"roles": {"tenant": [{"name": "Colaborador"}]}}})
        else:
            self._send(401, {"message": "unauth"})


class PostmanImporterTests(unittest.TestCase):
    def test_converts_common_assertions_and_extractions(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        self.assertEqual(col["nome"], "Demo")
        self.assertEqual(len(col["casos"]), 2)
        login = col["casos"][0]
        tipos = {(a.tipo, a.alvo, a.valor) for a in login.assercoes}
        self.assertIn(("status", "", "200"), tipos)
        self.assertIn(("json_type", "data.token", "string"), tipos)
        self.assertIn(("json_not_empty", "data.token", ""), tipos)
        self.assertIn(("json_equals", "data.token_type", "Bearer"), tipos)
        self.assertIn(("json_absent", "errors", ""), tipos)
        self.assertEqual([(e.nome, e.caminho) for e in login.extrair], [("auth_token", "data.token")])

    def test_folder_prefix_alias_and_loop_variable(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        me = col["casos"][1]
        self.assertTrue(me.nome.startswith("Pasta / "))
        self.assertIn(("json_type", "data.roles.tenant", "array"), {(a.tipo, a.alvo, a.valor) for a in me.assercoes})
        self.assertFalse(any(a.alvo.startswith("r.") for a in me.assercoes))
        self.assertTrue(any("ignorada" in w for w in me.avisos))

    def test_secret_detection_in_variables(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        by_name = {v["nome"]: v for v in col["variaveis"]}
        self.assertTrue(by_name["valid_password"]["secreto"])
        self.assertFalse(by_name["base_url"]["secreto"])

    def test_invalid_json_raises(self):
        with self.assertRaises(PostmanImportError):
            PostmanImporter.parse_collection(b"not json")


class ApiTestRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _casos(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        return col["casos"]

    def test_chains_extracted_token_into_next_case(self):
        runner = ApiTestRunner({"base_url": self.base, "valid_password": "s3cr3t"})
        res = runner.executar(self._casos())
        self.assertTrue(res[0].passou, [a.detalhe for a in res[0].assercoes if not a.passou])
        self.assertTrue(res[1].passou, [a.detalhe for a in res[1].assercoes if not a.passou])
        self.assertIn("eyJ", runner.variaveis["auth_token"])

    def test_wrong_password_fails_status_assertion(self):
        runner = ApiTestRunner({"base_url": self.base, "valid_password": "errada"})
        res = runner.executar(self._casos()[:1])
        self.assertFalse(res[0].passou)
        self.assertEqual(res[0].status_code, 401)

    def test_disabled_case_is_skipped_and_missing_assertions_fail(self):
        caso = ApiTestCase(id="x", nome="sem regra", metodo="GET", url=self.base + "/me")
        pulado = ApiTestCase(id="y", nome="pulado", metodo="GET", url=self.base + "/me", habilitado=False)
        res = ApiTestRunner({}).executar([caso, pulado])
        self.assertFalse(res[0].passou)
        self.assertIn("Nenhuma asserção", res[0].assercoes[0].descricao)
        self.assertTrue(res[1].pulado)

    def test_json_path_helpers(self):
        dado = {"errors": {"email": ["req"]}, "items": [{"id": 7}]}
        self.assertEqual(ApiTestRunner.obter_caminho(dado, "errors.email[0]"), "req")
        self.assertEqual(ApiTestRunner.obter_caminho(dado, "items[0].id"), 7)
        self.assertIs(ApiTestRunner.obter_caminho(dado, "items[3].id"), ApiTestRunner._AUSENTE)


class ApiEvidenceTests(unittest.TestCase):
    def test_masks_secrets_tokens_and_json_fields(self):
        texto = '{"password": "abc123", "token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"} Bearer xyz.token'
        saida = ApiEvidenceBuilder.mascarar(texto, ["abc123"])
        self.assertNotIn("abc123", saida)
        self.assertNotIn("eyJhbGci", saida)
        self.assertNotIn("xyz.token", saida)
        self.assertIn(MASCARA, saida)

    def test_markdown_and_zip_structure(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        runner = ApiTestRunner({"base_url": "http://127.0.0.1:1", "valid_password": "s3cr3t"}, timeout=1)
        res = runner.executar(col["casos"][:1])  # falha de conexão -> erro registrado, não exceção
        self.assertTrue(res[0].erro)
        md = ApiEvidenceBuilder.gerar_markdown("Proj", "Homologação", "http://127.0.0.1:1", res, segredos=["s3cr3t"])
        self.assertIn("# Relatório de Testes de API", md)
        self.assertNotIn("s3cr3t", md)
        z = ApiEvidenceBuilder.gerar_zip("Proj", res, md, segredos=["s3cr3t"])
        nomes = zipfile.ZipFile(__import__("io").BytesIO(z)).namelist()
        self.assertTrue(any(n.endswith("/RELATORIO.md") for n in nomes))
        self.assertTrue(any(n.endswith("/01_login/1_request.txt") for n in nomes))
        self.assertTrue(any(n.endswith("/indice.json") for n in nomes))


if __name__ == "__main__":
    unittest.main()


class ExternalExecutionTests(unittest.TestCase):
    """Respostas obtidas fora do runner (componente de navegador) avaliadas em Python."""

    def test_external_responses_are_evaluated_like_direct_execution(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        casos = col["casos"]
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
        respostas = [
            {"status": 200, "status_text": "OK", "headers": {"Content-Type": "application/json"},
             "body": json.dumps({"data": {"token": token, "token_type": "Bearer"}}), "tempo_ms": 120, "erro": ""},
            {"status": 200, "status_text": "OK", "headers": {"content-type": "application/json"},
             "body": json.dumps({"data": {"roles": {"tenant": [{"name": "Colaborador"}]}}}), "tempo_ms": 80, "erro": ""},
        ]
        runner = ApiTestRunner({"base_url": "http://x", "valid_password": "s3cr3t"})
        res = runner.avaliar_execucao_externa(casos, respostas)
        self.assertEqual([r.passou for r in res], [True, True], [[a.detalhe for a in r.assercoes] for r in res])
        self.assertEqual(runner.variaveis["auth_token"], token)
        self.assertEqual(res[1].request_headers["Authorization"], f"Bearer {token}")

    def test_external_network_error_and_disabled_case(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        casos = col["casos"]
        casos[1].habilitado = False
        res = ApiTestRunner({"base_url": "http://x"}).avaliar_execucao_externa(
            casos, [{"status": None, "erro": "Failed to fetch", "tempo_ms": 5}])
        self.assertEqual(res[0].resultado_label, "Erro")
        self.assertIn("Failed to fetch", res[0].erro)
        self.assertTrue(res[1].pulado)


class ApiDiscoveryTests(unittest.TestCase):
    def test_extracts_routes_and_builds_variants(self):
        from qa_testgen.infrastructure.api_discovery import extrair_rotas, montar_sondas
        rotas = extrair_rotas("Endpoint POST /api/auth/login. Depois GET /api/me. Também /api/v1/auth/logout")
        self.assertEqual(rotas[:2], [("POST", "/api/auth/login"), ("GET", "/api/me")])
        nomes = [s["nome"] for s in montar_sondas("POST /api/auth/login", "http://x/")]
        self.assertEqual(nomes, ["POST /api/auth/login", "POST /api/v1/auth/login"])
        self.assertTrue(montar_sondas("", "http://x")[0]["url"].startswith("http://x/api/v1/"))

    def test_analysis_detects_real_route_i18n_errors_and_protected(self):
        from qa_testgen.infrastructure.api_discovery import montar_sondas, analisar
        sondas = montar_sondas("POST /api/auth/login e GET /api/me", "http://x")
        resp = {
            "POST /api/auth/login": {"status": 405, "headers": {"Content-Type": "application/xml"}, "body": "<Error/>"},
            "POST /api/v1/auth/login": {"status": 422, "headers": {"Content-Type": "application/json"},
                                        "body": json.dumps({"message": "api.errors.validation_failed", "errors": {"email": ["req"]}})},
            "GET /api/me": {"status": 200, "headers": {"Content-Type": "text/html"}, "body": "<html>"},
            "GET /api/v1/me": {"status": 401, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"message": "api.errors.unauthenticated"})},
        }
        a = analisar(sondas, [resp[s["nome"]] for s in sondas])
        self.assertEqual(a["rotas_reais"], {"/api/auth/login": "/api/v1/auth/login", "/api/me": "/api/v1/me"})
        self.assertIn("CHAVE i18n", a["observacoes"])
        self.assertIn("errors.<campo>", a["observacoes"])
        self.assertIn("/api/v1/me", a["observacoes"].split("Rotas protegidas")[1])


class ApiToAssistantTests(unittest.TestCase):
    def _bateria(self):
        col = PostmanImporter.parse_collection(json.dumps(COLLECTION).encode())
        casos = [c.to_dict() for c in col["casos"]]
        from qa_testgen.domain.models.api_test import ApiCaseResult, ApiAssertionResult
        res = [ApiCaseResult(case_id=casos[0]["id"], nome=casos[0]["nome"], metodo="POST", url_final="u", request_headers={},
                             request_body="", status_code=200, status_text="OK", response_headers={}, response_body="{}", tempo_ms=120,
                             assercoes=[ApiAssertionResult("Status 200", True), ApiAssertionResult("token", False, "ausente")]),
               ApiCaseResult(case_id=casos[1]["id"], nome=casos[1]["nome"], metodo="GET", url_final="u", request_headers={},
                             request_body="", status_code=None, status_text="", response_headers={}, response_body="", tempo_ms=0, pulado=True)]
        return casos, res

    def test_converts_to_matrix_cases_and_plan_per_endpoint(self):
        from qa_testgen.infrastructure.api_to_assistant import converter_bateria
        casos, res = self._bateria()
        out = converter_bateria("Login", "Homologação", "http://x", casos, res,
                                variaveis=[{"nome": "valid_password", "secreto": True}, {"nome": "base_url", "secreto": False}],
                                work_items=[{"id": 7040, "title": "RF-02"}], incluir_resultado=True, mc_inicio=3)
        self.assertEqual([m["id"] for m in out["matriz"]], ["MC-003 HML", "MC-004 HML"])
        self.assertEqual(out["matriz"][0]["funcionalidade"], "POST /login")
        tc = out["test_cases"][0]
        self.assertEqual(tc["requisitos_relacionados"], ["MC-003 HML"])
        self.assertEqual(tc["work_item_relacionado"], "7040")
        self.assertIn("Última execução (HML", tc["pre_condicoes"])
        self.assertIn("Reprovado — 1/2", tc["pre_condicoes"])
        self.assertNotIn("{{valid_password}}", tc["passos"][0]["acao"])      # segredo mascarado no passo
        self.assertIn("HTTP 200", tc["passos"][0]["resultado_esperado"])
        self.assertEqual(tc["passos"][1]["acao"], "Guardar 'data.token' da resposta como {{auth_token}}")
        plano = out["test_plans"][0]
        self.assertEqual([s["nome"] for s in plano["suites"]], ["POST /login", "GET /me"])
        self.assertEqual(plano["suites"][0]["casos"], [casos[0]["nome"]])
        # sem resultado no texto
        out2 = converter_bateria("Login", "Homologação", "http://x", casos, res, incluir_resultado=False)
        self.assertNotIn("Última execução", out2["test_cases"][0]["pre_condicoes"])

    def test_results_for_test_run(self):
        from qa_testgen.infrastructure.api_to_assistant import resultados_para_test_run
        casos, res = self._bateria()
        m = resultados_para_test_run(casos, res)
        self.assertEqual(set(m), {casos[0]["nome"]})            # o pulado não entra
        self.assertEqual(m[casos[0]["nome"]]["outcome"], "Failed")
        self.assertIn("token: ausente", m[casos[0]["nome"]]["comentario"])
