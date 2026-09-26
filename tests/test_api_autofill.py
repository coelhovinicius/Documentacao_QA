import json
import unittest

from qa_testgen.infrastructure import api_autofill as autofill

LOGIN = {"id": "login", "nome": "1. Login - sucesso (200)", "metodo": "POST", "habilitado": True,
         "url": "{{base_url}}/api/v1/auth/login", "headers": {"Accept": "application/json"},
         "body": '{"email": "{{valid_email}}", "password": "{{valid_password}}"}',
         "assercoes": [{"tipo": "status", "alvo": "", "valor": "200", "descricao": ""}],
         "extrair": [{"nome": "auth_token", "caminho": "data.token"}]}


def _caso(cid, token_var, habilitado=True):
    return {"id": cid, "nome": cid, "metodo": "GET", "habilitado": habilitado, "body": "",
            "url": "{{base_url}}/api/v1/psychosocial-surveys", "headers": {"Authorization": "Bearer {{" + token_var + "}}"},
            "assercoes": [{"tipo": "status", "valor": "200"}], "extrair": []}


VARS = [{"nome": "valid_email", "valor": "qa@x.com", "secreto": False},
        {"nome": "valid_password", "valor": "", "secreto": True},
        {"nome": "gestor_token", "valor": "", "secreto": True},
        {"nome": "invalid_token", "valor": "", "secreto": True}]


class ValorNegativoTest(unittest.TestCase):
    def test_nomes_de_teste_negativo(self):
        self.assertEqual(autofill.valor_negativo("invalid_token"), autofill.VALOR_TOKEN_INVALIDO)
        self.assertEqual(autofill.valor_negativo("token_expirado"), autofill.VALOR_TOKEN_INVALIDO)
        self.assertEqual(autofill.valor_negativo("nonexistent_email"), autofill.VALOR_EMAIL_INEXISTENTE)
        self.assertEqual(autofill.valor_negativo("invalid_email"), autofill.VALOR_EMAIL_INVALIDO)
        self.assertEqual(autofill.valor_negativo("wrong_password"), autofill.VALOR_SENHA_ERRADA)
        self.assertEqual(autofill.valor_negativo("survey_id_inexistente"), autofill.VALOR_ID_INEXISTENTE)

    def test_nomes_validos_nao_sao_tocados(self):
        for nome in ("valid_email", "valid_password", "gestor_token", "auth_token", "survey_id", "invalid"):
            self.assertIsNone(autofill.valor_negativo(nome), nome)


class PerfilDoTokenTest(unittest.TestCase):
    def test_perfis(self):
        self.assertEqual(autofill.perfil_do_token("gestor_token"), "gestor")
        self.assertEqual(autofill.perfil_do_token("admin_access_token"), "admin")
        self.assertEqual(autofill.perfil_do_token("auth_token"), "")
        self.assertEqual(autofill.perfil_do_token("survey_id"), "")


class CompletarBateriaTest(unittest.TestCase):
    def test_cria_login_do_gestor_antes_do_primeiro_uso_e_preenche_negativos(self):
        casos = [LOGIN, _caso("colab", "auth_token"), _caso("gestao1", "gestor_token"),
                 _caso("gestao2", "gestor_token"), _caso("invalido", "invalid_token")]
        novos, variaveis, relatorio = autofill.completar_bateria(casos, VARS)
        self.assertEqual([c["id"] for c in casos][2], "gestao1")          # entrada intacta
        self.assertEqual(len(novos), 6)
        login_gestor = novos[2]
        self.assertEqual(novos[3]["id"], "gestao1")
        self.assertEqual(login_gestor["url"], LOGIN["url"])
        self.assertEqual(json.loads(login_gestor["body"]), {"email": "{{gestor_email}}", "password": "{{gestor_password}}"})
        self.assertEqual(login_gestor["extrair"], [{"nome": "gestor_token", "caminho": "data.token"}])
        por_nome = {v["nome"]: v for v in variaveis}
        self.assertEqual(por_nome["gestor_email"], {"nome": "gestor_email", "valor": "", "secreto": False})
        self.assertTrue(por_nome["gestor_password"]["secreto"])
        self.assertEqual(por_nome["invalid_token"], {"nome": "invalid_token", "valor": autofill.VALOR_TOKEN_INVALIDO, "secreto": False})
        self.assertEqual(len(relatorio), 2)

    def test_segunda_vez_nao_faz_nada(self):
        casos, variaveis, _ = autofill.completar_bateria([LOGIN, _caso("g", "gestor_token")], VARS)
        de_novo, vars_de_novo, relatorio = autofill.completar_bateria(casos, variaveis)
        self.assertEqual((de_novo, vars_de_novo, relatorio), (casos, variaveis, []))

    def test_token_ja_preenchido_a_mao_nao_gera_login(self):
        casos, _, relatorio = autofill.completar_bateria([LOGIN, _caso("g", "gestor_token")], VARS, {"gestor_token": "abc"})
        self.assertEqual(len(casos), 2)
        self.assertFalse(any("Login (gestor)" in r for r in relatorio))

    def test_caso_desabilitado_nao_pede_login(self):
        casos, _, _ = autofill.completar_bateria([LOGIN, _caso("g", "gestor_token", habilitado=False)], VARS)
        self.assertEqual(len(casos), 2)

    def test_sem_login_de_modelo_explica_e_nao_cria(self):
        casos, _, relatorio = autofill.completar_bateria([_caso("g", "gestor_token")], VARS)
        self.assertEqual(len(casos), 1)
        self.assertTrue(any("não tem um caso de login" in r for r in relatorio))


def _resp(caso_id, corpo, status=200, pulado=False):
    from types import SimpleNamespace
    return SimpleNamespace(case_id=caso_id, status_code=status, pulado=pulado, response_body=json.dumps(corpo))


class DescobrirValoresTest(unittest.TestCase):
    def setUp(self):
        self.casos, _, _ = autofill.completar_bateria(
            [LOGIN, _caso("g", "gestor_token"),
             dict(_caso("dash", "auth_token"), url="{{base_url}}/api/v1/dashboard?department_id={{department_id}}"),
             dict(_caso("gdash", "gestor_token"), url="{{base_url}}/api/v1/dashboard?department_id={{gestor_department_id}}"),
             dict(_caso("outro", "gestor_token"), url="{{base_url}}/api/v1/dashboard?department_id={{other_department_id}}"),
             dict(LOGIN, id="inativo", body='{"email": "{{inactive_email}}", "password": "x"}', extrair=[])],
            VARS)
        self.login_gestor = next(c for c in self.casos if c["nome"].startswith("Login (gestor)"))
        self.faltantes = ["department_id", "gestor_department_id", "other_department_id", "inactive_email"]

    def test_acha_cada_id_na_resposta_do_perfil_certo(self):
        resultados = [_resp("login", {"data": {"token": "t1", "user": {"department_id": 7}}}),
                      _resp(self.login_gestor["id"], {"data": {"token": "t2", "user": {"department": {"id": 3, "name": "RH"}}}})]
        achados, nao = autofill.descobrir_valores(self.casos, resultados, self.faltantes)
        por_var = {a["var"]: a for a in achados}
        self.assertEqual((por_var["department_id"]["caminho"], por_var["department_id"]["valor"]), ("data.user.department_id", "7"))
        self.assertEqual((por_var["gestor_department_id"]["caminho"], por_var["gestor_department_id"]["valor"]), ("data.user.department.id", "3"))
        self.assertEqual({a["modo"] for a in achados}, {"extrair"})   # os logins rodam antes de quem usa
        motivos = dict(nao)
        self.assertIn("valor diferente", motivos["other_department_id"])
        self.assertIn("credencial", motivos["inactive_email"])

        casos, variaveis = autofill.aplicar_descobertas(self.casos, VARS, achados)
        login = next(c for c in casos if c["id"] == "login")
        self.assertIn({"nome": "department_id", "caminho": "data.user.department_id"}, login["extrair"])
        self.assertIn({"nome": "department_id", "valor": "7", "secreto": False}, variaveis)

    def test_nao_usa_resposta_de_outro_perfil_nem_resposta_de_erro(self):
        resultados = [_resp("login", {"data": {"token": "t1"}}),
                      _resp(self.login_gestor["id"], {"data": {"user": {"department_id": 3}}}, status=401)]
        achados, nao = autofill.descobrir_valores(self.casos, resultados, ["department_id", "gestor_department_id"])
        self.assertEqual(achados, [])
        self.assertEqual({v for v, _ in nao}, {"department_id", "gestor_department_id"})


if __name__ == "__main__":
    unittest.main()
