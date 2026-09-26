import json
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

from qa_testgen.domain.models.api_test import ApiAssertion, ApiTestCase
from qa_testgen.infrastructure.api_evidence import ApiEvidenceBuilder, MASCARA
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner, CaminhoInvalido
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

    def test_dependent_case_is_blocked_when_upstream_extraction_fails(self):
        # caso 1 falha (senha errada) e não extrai auth_token; caso 2 usa {{auth_token}} -> Bloqueado
        runner = ApiTestRunner({"base_url": self.base, "valid_password": "errada", "auth_token": ""})
        res = runner.executar(self._casos())
        self.assertFalse(res[0].passou)
        self.assertTrue(res[1].bloqueado)
        self.assertEqual(res[1].resultado_label, "Bloqueado")
        self.assertIn("'auth_token'", res[1].motivo_pulo)
        self.assertIn("\"Login\"", res[1].motivo_pulo)
        self.assertEqual(res[1].status_code, None)   # não mandou a requisição
        # variável preenchida à mão pela pessoa -> não bloqueia
        runner2 = ApiTestRunner({"base_url": self.base, "valid_password": "errada", "auth_token": "manual"})
        res2 = runner2.executar(self._casos())
        self.assertFalse(res2[1].bloqueado)
        self.assertEqual(res2[1].status_code, 401)
        # variável que ninguém extrai e ficou sem valor (preenchimento pendente) -> bloqueia só este caso,
        # sem mandar a requisição, com o motivo apontando a seção Variáveis
        caso = ApiTestCase(id="z", nome="sem produtor", metodo="GET", url=self.base + "/me",
                           headers={"Authorization": "Bearer {{outra}}"}, assercoes=[ApiAssertion(tipo="status", valor="401")])
        res3 = ApiTestRunner({"base_url": self.base}).executar([caso])[0]
        self.assertTrue(res3.bloqueado)
        self.assertIn("'outra' está sem valor", res3.motivo_pulo)
        self.assertEqual(res3.status_code, None)
        # com valor -> roda normalmente
        self.assertFalse(ApiTestRunner({"base_url": self.base, "outra": "x"}).executar([caso])[0].bloqueado)

    def test_external_blocked_case_is_evaluated_like_python(self):
        casos = self._casos()
        respostas = [
            {"status": 401, "status_text": "Unauthorized", "headers": {}, "body": "{\"message\": \"invalid\"}", "tempo_ms": 5},
            {"status": None, "bloqueado": "variável vazia: auth_token", "tempo_ms": 0},
        ]
        res = ApiTestRunner({"base_url": self.base, "valid_password": "errada", "auth_token": ""}).avaliar_execucao_externa(casos, respostas)
        self.assertTrue(res[1].bloqueado)
        self.assertIn("'auth_token'", res[1].motivo_pulo)

    def test_json_path_helpers(self):
        dado = {"errors": {"email": ["req"]}, "items": [{"id": 7}]}
        self.assertEqual(ApiTestRunner.obter_caminho(dado, "errors.email[0]"), "req")
        self.assertEqual(ApiTestRunner.obter_caminho(dado, "items[0].id"), 7)
        self.assertIs(ApiTestRunner.obter_caminho(dado, "items[3].id"), ApiTestRunner._AUSENTE)

    def test_jsonpath_subset_used_by_ai_generated_batteries(self):
        # caminhos reais que a IA gerou na bateria de 25/09 e que antes davam "campo ausente"
        dado = {"data": [{"id": 1, "name": "Demandas", "dimension": {"id": 9}}, {"id": 2, "name": "Cargo"}],
                "meta": {"total": 2}, "a b": {"c": 3}}
        oc = ApiTestRunner.obter_caminho
        self.assertEqual(oc(dado, "data.length"), 2)
        self.assertEqual(oc(dado, "$.meta.total"), 2)
        self.assertEqual(oc(dado, "data[-1].name"), "Cargo")
        self.assertEqual(oc(dado, "['a b'].c"), 3)
        self.assertEqual(list(oc(dado, "data[*].id")), [1, 2])
        self.assertEqual(list(oc(dado, "data[?(@.name=='Demandas')].name")), ["Demandas"])
        self.assertEqual(list(oc(dado, "data[?(@.dimension.id == 9)].id")), [1])
        self.assertEqual(list(oc(dado, "data[?(@.name=='Nada')].id")), [])
        for invalido in ("data..id", "data[?(@.id == )].x", "data[abc]"):
            with self.assertRaises(CaminhoInvalido, msg=invalido):
                oc(dado, invalido)

    def test_assertions_with_many_values_and_unsupported_syntax(self):
        runner = ApiTestRunner({"base_url": "http://x", "plan_id": "2"})

        class R:
            status_code, reason, headers, text = 200, "OK", {}, ""
        corpo = {"data": [{"id": 1, "email": None}, {"id": 2}]}
        a = lambda tipo, alvo, valor="": runner._avaliar(ApiAssertion(tipo=tipo, alvo=alvo, valor=valor), R(), corpo, 10)
        self.assertTrue(a("json_equals", "data.length", "2").passou)
        self.assertTrue(a("json_contains", "data[*].id", "{{plan_id}}").passou)
        self.assertTrue(a("json_type", "data[*].id", "number").passou)
        self.assertTrue(a("json_absent", "data[?(@.id==3)]").passou)
        self.assertFalse(a("json_exists", "data[?(@.id==3)]").passou)
        erro = a("json_exists", "data[?(@.id == )].riskClassification")
        self.assertFalse(erro.passou)
        self.assertIn("sintaxe de caminho não suportada", erro.detalhe)
        self.assertNotIn("ausente", erro.detalhe)


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

    def test_zip_definition_keeps_header_templates_but_hides_secret_values(self):
        definicao = json.dumps({"casos": [{"headers": {"Authorization": "Bearer {{auth_token}}"}}],
                                "variaveis": [{"nome": "valid_password", "valor": "s3cr3t"}]})
        z = ApiEvidenceBuilder.gerar_zip("Proj", [], "# md", segredos=["s3cr3t"], definicao_json=definicao)
        zf = zipfile.ZipFile(__import__("io").BytesIO(z))
        conteudo = zf.read([n for n in zf.namelist() if n.endswith("definicao-testes.json")][0]).decode()
        self.assertIn("Bearer {{auth_token}}", conteudo)
        self.assertNotIn("s3cr3t", conteudo)


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
        res = ApiTestRunner({"base_url": "http://x", "valid_email": "qa@x.com", "valid_password": "s3cr3t"}).avaliar_execucao_externa(
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

    def test_same_endpoint_with_other_ids_or_query_is_one_suite(self):
        # bateria de 25/09: /dashboard com {{survey_id}}, com {{invalid_survey_id}} = "kjdafakjhfadksj" e com
        # ?tenant_department_id=... virava 3 suítes diferentes
        from qa_testgen.infrastructure.api_to_assistant import converter_bateria
        base = "https://api.x"

        def caso(cid, url):
            return {"id": cid, "nome": f"caso {cid}", "metodo": "GET", "url": "{{base_url}}" + url, "headers": {},
                    "body": "", "habilitado": True, "assercoes": [{"tipo": "status", "valor": "200"}], "extrair": []}
        casos = [caso("a", "/api/v1/surveys/{{survey_id}}/dashboard"),
                 caso("b", "/api/v1/surveys/{{invalid_survey_id}}/dashboard"),
                 caso("c", "/api/v1/surveys/{{survey_id}}/dashboard?tenant_department_id={{department_id}}"),
                 caso("d", "/api/v1/action-plans/999999999/close")]
        out = converter_bateria("X", "Homologação", base, casos, [],
                                variaveis=[{"nome": "invalid_survey_id", "valor": "kjdafakjhfadksj", "secreto": False}])
        suites = out["test_plans"][0]["suites"]
        self.assertEqual([s["nome"] for s in suites], ["GET /api/v1/surveys/{id}/dashboard", "GET /api/v1/action-plans/{id}/close"])
        self.assertEqual(suites[0]["casos"], ["caso a", "caso b", "caso c"])
        self.assertEqual(out["matriz"][1]["funcionalidade"], "GET /api/v1/surveys/{id}/dashboard")
        # o passo continua com a URL concreta (reproduzível)
        self.assertIn("/surveys/kjdafakjhfadksj/dashboard", out["test_cases"][1]["passos"][0]["acao"])
        self.assertIn("?tenant_department_id=", out["test_cases"][2]["passos"][0]["acao"])

    def test_last_run_uses_execution_time_and_negative_by_name(self):
        from qa_testgen.domain.models.api_test import ApiCaseResult
        from qa_testgen.infrastructure.api_to_assistant import converter_bateria
        casos = [{"id": "a", "nome": "3. Gestor tenta filtrar setor fora do escopo (200 sem dados)", "metodo": "GET",
                  "url": "{{base_url}}/x", "headers": {}, "body": "", "habilitado": True, "extrair": [],
                  "assercoes": [{"tipo": "status", "valor": "200"}, {"tipo": "response_time_max", "valor": "500"}]},
                 {"id": "b", "nome": "1. Sucesso - listar", "metodo": "GET", "url": "{{base_url}}/x", "headers": {},
                  "body": "", "habilitado": True, "extrair": [], "assercoes": [{"tipo": "status", "valor": "200"}]}]
        res = [ApiCaseResult(case_id="a", nome=casos[0]["nome"], metodo="GET", url_final="https://api.x/x", request_headers={},
                             request_body="", status_code=200, status_text="OK", response_headers={}, response_body="{}",
                             tempo_ms=120)]
        out = converter_bateria("X", "Homologação", "https://api.x", casos, res, executado_em="25/09/2026 17:31")
        self.assertIn("Última execução (HML, 25/09/2026 17:31)", out["test_cases"][0]["pre_condicoes"])
        self.assertIn("tempo de resposta até 500 ms", out["test_cases"][0]["passos"][0]["resultado_esperado"])
        self.assertEqual([m["categoria"] for m in out["matriz"]], ["Negativo", "Positivo"])
        # sem o horário da execução, não inventa (antes saía a hora da conversão)
        out2 = converter_bateria("X", "Homologação", "https://api.x", casos, res)
        self.assertIn("Última execução (HML): ", out2["test_cases"][0]["pre_condicoes"])

    def test_results_for_test_run(self):
        from qa_testgen.infrastructure.api_to_assistant import resultados_para_test_run
        casos, res = self._bateria()
        m = resultados_para_test_run(casos, res)
        self.assertEqual(set(m), {casos[0]["nome"]})            # o pulado não entra
        self.assertEqual(m[casos[0]["nome"]]["outcome"], "Failed")
        self.assertIn("token: ausente", m[casos[0]["nome"]]["comentario"])
