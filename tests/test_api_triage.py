import json
import unittest

from qa_testgen.infrastructure import api_contracts as ct
from qa_testgen.infrastructure import api_triage as tri
from qa_testgen.infrastructure.api_test_runner import ApiTestRunner

BASE = "https://api.exemplo.test"
SENHA = "S3nh@Secreta"


def caso(cid, nome, metodo, rota, status, body="", auth=True, extrair=None, assercoes=None):
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer {{auth_token}}"
    return {"id": cid, "nome": nome, "metodo": metodo, "url": "{{base_url}}" + rota, "headers": headers, "body": body,
            "habilitado": True, "descricao": "", "extrair": extrair or [],
            "assercoes": [{"tipo": "status", "alvo": "", "valor": str(status), "descricao": ""}] + list(assercoes or [])}


def resp(status, corpo, ms=120):
    return {"status": status, "status_text": "", "headers": {"content-type": "application/json"},
            "body": json.dumps(corpo), "tempo_ms": ms}


LOGIN = caso("login", "1. Login (200)", "POST", "/api/v1/auth/login", 200, auth=False,
             body='{"email": "{{email}}", "password": "{{senha}}"}', extrair=[{"nome": "auth_token", "caminho": "data.token"}])


def executar(casos, respostas):
    runner = ApiTestRunner({"base_url": BASE, "email": "qa@x.test", "senha": SENHA})
    return runner.avaliar_execucao_externa(casos, respostas)


class TriagemTest(unittest.TestCase):
    def setUp(self):
        self.casos = [
            LOGIN,
            caso("perfil", "2. Dimensões (200)", "GET", "/api/v1/dimensions", 200),
            caso("camel", "3. Criar pesquisa (201)", "POST", "/api/v1/surveys", 201, body='{"startDate": "2026-10-01"}'),
            caso("semauth", "4. Criar rodada (201)", "POST", "/api/v1/rounds", 201, auth=False, body='{"a": 1}'),
            caso("senha", "5. Senha formato inválido (400)", "POST", "/api/v1/auth/login", 400, auth=False,
                 body='{"email": "{{email}}", "password": "x"}'),
            caso("vaza", "6. Pesquisa inexistente (404)", "GET", "/api/v1/surveys/abc/dashboard", 404,
                 assercoes=[{"tipo": "json_exists", "alvo": "error.message", "valor": ""}]),
            caso("cinco", "7. Relatório (200)", "GET", "/api/v1/report", 200),
            caso("aberta", "8. Relatório sem token (401)", "GET", "/api/v1/report", 401, auth=False),
            caso("valid", "9. E-mail inválido recusado (422)", "POST", "/api/v1/users", 422, body='{"email": "x"}'),
            caso("dep", "10. Ativar pesquisa (200)", "PATCH", "/api/v1/surveys/{{survey_id}}/activate", 200),
        ]
        self.casos[2]["extrair"] = [{"nome": "survey_id", "caminho": "data.id"}]
        respostas = [
            resp(200, {"data": {"token": "tok-123", "user": {"id": 1}}}),
            resp(403, {"message": "api.errors.collaborator_required"}),
            resp(422, {"message": "api.errors.validation_failed", "errors": {"start_date": ["The start date field is required."]}}),
            resp(401, {"message": "api.errors.unauthenticated"}),
            resp(401, {"message": "api.auth.invalid_credentials"}),
            resp(404, {"message": "No query results for model [App\\Models\\V1\\Survey] abc"}),
            resp(500, {"message": "Server Error"}),
            resp(200, {"data": [{"id": 1}]}),
            resp(201, {"data": {"id": 9}}),
            {"bloqueado": "x"},
        ]
        self.resultados = executar(self.casos, respostas)
        self.ctx = {"base_url": BASE, "ambiente": "Homologação", "projeto": "Bateria X", "usuario_teste": "qa@x.test",
                    "responsaveis": [{"id": 8402, "titulo": "US", "responsavel": "Fulana Dev"}]}
        self.an = tri.analisar(self.casos, self.resultados, self.ctx)
        self.cat = {i["case_id"]: i["categoria"] for i in self.an["itens"]}

    def test_categorias(self):
        self.assertEqual(self.cat["perfil"], "perfil")
        self.assertEqual(self.cat["camel"], "bateria_requisicao")
        self.assertEqual(self.cat["semauth"], "bateria_auth")
        self.assertEqual(self.cat["senha"], "a_confirmar")        # login não pede token: 401 aqui é regra, não bateria
        self.assertEqual(self.cat["vaza"], "bug_api")            # faz parte do achado de vazamento
        self.assertEqual(self.cat["cinco"], "bug_api")
        self.assertEqual(self.cat["aberta"], "bug_api")
        self.assertEqual(self.cat["valid"], "bug_api")
        self.assertEqual(self.cat["dep"], "bloqueado")

    def test_achados_com_orientacao_completa(self):
        por_tipo = {a["id"]: a for a in self.an["achados"]}
        self.assertEqual(set(por_tipo), {"vazamento_interno", "erro_servidor", "acesso_indevido", "validacao_ausente"})
        self.assertEqual(self.an["achados"][0]["id"], "acesso_indevido")      # Crítica vem primeiro
        vaz = por_tipo["vazamento_interno"]
        self.assertEqual(vaz["gravidade"], "Baixa")
        self.assertIn("App\\Models\\V1\\Survey", vaz["evidencia"])
        for campo in ("analise", "como_confirmar", "o_que_fazer", "com_quem_falar", "mensagens"):
            self.assertTrue(vaz[campo], campo)
        self.assertIn("Fulana Dev", vaz["com_quem_falar"][0]["quem"])        # nome real do responsável do Work Item
        self.assertIn("api.errors.not_found", vaz["mensagens"][0]["texto"])

    def test_orientacoes_de_perfil_e_cascata(self):
        por_cat = {o["categoria"]: o for o in self.an["orientacoes"]}
        self.assertIn("qa@x.test", por_cat["perfil"]["resumo"])
        self.assertTrue(por_cat["perfil"]["mensagens"][0]["texto"])
        self.assertTrue(any("survey_id" in l and "3. Criar pesquisa" in l for l in por_cat["bloqueado"]["itens"]))
        self.assertIn("a_confirmar", por_cat)

    def test_card_decide_o_que_e_requisito(self):
        # o card não fala em 400: o 401 do login deixa de ser "a confirmar" e vira presunção da bateria
        an = tri.analisar(self.casos, self.resultados, {**self.ctx, "especificacao": "O login devolve 401 para credenciais inválidas."})
        self.assertEqual({i["case_id"]: i["categoria"] for i in an["itens"]}["senha"], "bateria_formato")

    def test_rascunho_de_bug_mascarado_e_com_evidencia(self):
        vaz = next(a for a in self.an["achados"] if a["id"] == "vazamento_interno")
        rb = tri.rascunho_de_achado(vaz, self.casos, self.resultados, self.ctx, [SENHA, "tok-123"])
        self.assertTrue(rb["titulo"].startswith("[API][HML] "))
        self.assertEqual(rb["severidade"], "4 - Low")
        self.assertTrue(any("Autenticar" in p for p in rb["passos"]))
        # o número do caso é o do relatório da execução; o nome vai junto porque o CTxx do Azure pode ser outro
        self.assertIn("Casos (número no relatório da execução — nome do caso): ", rb["descricao"])
        self.assertIn(self.casos[5]["nome"], rb["descricao"])
        self.assertFalse(any("tok-123" in p for p in rb["passos"]))
        arquivos = tri.arquivos_de_evidencia(rb, self.resultados, [SENHA, "tok-123"])
        self.assertEqual(len(arquivos), 1)
        conteudo = arquivos[0][1].decode("utf-8")
        self.assertIn("=== RESPONSE ===", conteudo)
        self.assertNotIn("tok-123", conteudo)

    def test_relatorio_antes_do_azure_traz_rascunhos_mascarados_e_correcoes(self):
        vaz = next(a for a in self.an["achados"] if a["id"] == "vazamento_interno")
        rb = tri.rascunho_de_achado(vaz, self.casos, self.resultados, self.ctx, [SENHA])
        rb["descricao"] += f" (senha usada: {SENHA})"     # alguém colou a senha ao editar
        enviado = {**rb, "id": "x", "status": "enviado", "azure_id": 9001, "azure_url": "https://az/9001", "titulo": "Outro"}
        bugs = [rb, enviado]
        md = "\n".join(tri.markdown_da_analise(self.an, bugs, casos=self.casos, correcoes=["caso 3: renomear campos"], segredos=[SENHA]))
        self.assertIn("Rascunho — ainda não enviado ao Azure DevOps", md)
        self.assertIn("Enviado ao Azure DevOps — Bug #9001", md)
        self.assertIn("1 rascunho(s) ainda não enviado(s)", md)
        self.assertIn("caso 6", md)                      # número do caso de evidência
        self.assertIn("O que dizer (para", md)
        self.assertIn("caso 3: renomear campos", md)
        self.assertNotIn(SENHA, md)
        d = tri.linhas_relatorio(self.an, bugs, casos=self.casos, segredos=[SENHA])
        self.assertEqual([b["enviado"] for b in d["rascunhos"]], [False, True])
        self.assertNotIn(SENHA, d["rascunhos"][0]["descricao"])

    def test_rascunho_de_caso(self):
        r = next(x for x in self.resultados if x.case_id == "senha")
        rb = tri.rascunho_de_caso(self.casos[4], r, self.casos, self.ctx, [SENHA], "O card exige 400.")
        self.assertIn("HTTP 400", rb["esperado"])
        self.assertIn("HTTP 401", rb["obtido"])
        self.assertFalse(any(SENHA in p for p in rb["passos"]))


class ContratosTest(unittest.TestCase):
    def setUp(self):
        self.casos = [
            LOGIN,
            caso("me", "2. Me (200)", "GET", "/api/v1/me", 200,
                 assercoes=[{"tipo": "json_exists", "alvo": "data.user.email", "valor": ""},
                            {"tipo": "json_exists", "alvo": "data.user.id", "valor": ""}]),
            caso("camel", "3. Criar (201)", "POST", "/api/v1/surveys", 201, body='{"startDate": "2026-10-01", "frequency": "anual"}'),
            caso("semauth", "4. Rodada (201)", "POST", "/api/v1/rounds", 201, auth=False),
            caso("msg", "5. Login errado (401)", "POST", "/api/v1/auth/login", 401, auth=False,
                 body='{"email": "a@a.test", "password": "x"}',
                 extrair=[{"nome": "error_msg", "caminho": "message"}],
                 assercoes=[{"tipo": "json_equals_var", "alvo": "message", "valor": "error_msg"}]),
        ]
        respostas = [
            resp(200, {"data": {"token": "t", "user": {"id": 1}}}),
            resp(200, {"data": {"id": 1, "email": "qa@x.test", "roles": {"tenant": []}}}),
            resp(422, {"message": "api.errors.validation_failed",
                       "errors": {"start_date": ["The start date field is required."], "frequency": ["The selected frequency is invalid."]}}),
            resp(401, {"message": "api.errors.unauthenticated"}),
            resp(401, {"message": "api.auth.invalid_credentials"}),
        ]
        self.resultados = executar(self.casos, respostas)

    def test_aprende_formato_e_vira_prompt(self):
        cont = ct.aprender({}, self.casos, self.resultados, BASE, "25/09/2026")
        post = cont["POST /api/v1/surveys"]
        self.assertIn("start_date", post["valida"])
        self.assertEqual(post["recusados"], {"frequency": ["anual"]})
        self.assertIn("data.email", cont["GET /api/v1/me"]["resposta_2xx"])
        prompt = ct.para_prompt(cont, "surveys")
        self.assertIn("start_date", prompt)
        self.assertIn("frequency=anual", prompt)
        self.assertIn("api.errors.unauthenticated", prompt)
        self.assertIn("api.auth.invalid_credentials", prompt)     # as duas mensagens de 401 aparecem

    def test_sugestoes_so_de_formato(self):
        sug = ct.sugerir_correcoes(self.casos, self.resultados)
        por = {(s["case_id"], s["tipo"]) for s in sug}
        self.assertIn(("me", "corrigir_caminho"), por)
        self.assertIn(("camel", "renomear_campos"), por)
        self.assertIn(("semauth", "add_auth"), por)
        self.assertIn(("msg", "circular"), por)
        self.assertNotIn(("msg", "add_auth"), por)                # login nunca ganha token
        novos, feito = ct.aplicar_correcoes(self.casos, sug)
        self.assertEqual([a["alvo"] for a in novos[1]["assercoes"][1:]], ["data.email", "data.id"])
        self.assertEqual(json.loads(novos[2]["body"]), {"start_date": "2026-10-01", "frequency": "anual"})
        self.assertEqual(novos[3]["headers"]["Authorization"], "Bearer {{auth_token}}")
        self.assertEqual(novos[4]["assercoes"][1]["tipo"], "json_not_empty")
        # status esperado NUNCA muda
        self.assertEqual([c["assercoes"][0] for c in novos], [c["assercoes"][0] for c in self.casos])
        self.assertTrue(feito)

    def test_caminho_ambiguo_nao_e_sugerido(self):
        self.assertEqual(ct._melhor_caminho("x.nome", {"a": {"nome": 1}, "b": {"nome": 2}}), "")
        self.assertEqual(ct._melhor_caminho("data.user.id", {"data": {"id": 1, "roles": [{"id": 2}]}}), "data.id")


if __name__ == "__main__":
    unittest.main()
