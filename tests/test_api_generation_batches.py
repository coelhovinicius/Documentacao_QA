import unittest

from qa_testgen.infrastructure import api_generation_batches as lotes_ia


def _caso(metodo, url, status, body="", nome="caso", assercoes=None, extrair=None):
    return {"nome": nome, "metodo": metodo, "url": url, "body": body,
            "assercoes": [{"tipo": "status", "alvo": "", "valor": status}] + list(assercoes or []),
            "extrair": list(extrair or [])}


LOGIN = dict(metodo="POST", url="{{base_url}}/api/v1/auth/login", status="200",
             extrair=[{"nome": "auth_token", "caminho": "data.token"}])


class DividirEmLotesTest(unittest.TestCase):
    def test_divide_na_ordem(self):
        self.assertEqual(lotes_ia.dividir_em_lotes([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_tamanho_invalido_vira_um(self):
        self.assertEqual(lotes_ia.dividir_em_lotes([1, 2], 0), [[1], [2]])


class JuntarRespostasTest(unittest.TestCase):
    def test_login_repetido_entre_lotes_fica_uma_vez_com_assercoes_somadas(self):
        r1 = {"nome_sugerido": "Pesquisa", "casos": [
            _caso(**LOGIN, body='{"email": "{{email}}", "password": "{{password}}"}', nome="1. Login"),
            _caso("GET", "{{base_url}}/api/v1/psychosocial-surveys/current", "200")]}
        r2 = {"nome_sugerido": "Outro", "casos": [
            # mesmo login, body com outra formatação e outro nome, mais uma asserção
            _caso(**LOGIN, body={"password": "{{password}}", "email": "{{email}}"}, nome="Login ok",
                  assercoes=[{"tipo": "existe", "alvo": "data.token", "valor": ""}]),
            _caso("GET", "{{base_url}}/api/v1/psychosocial-surveys/my-result", "200")]}
        junto = lotes_ia.juntar_respostas([r1, r2])
        self.assertEqual([c["url"].rsplit("/", 1)[-1] for c in junto["casos"]], ["login", "current", "my-result"])
        self.assertEqual(junto["nome_sugerido"], "Pesquisa")
        login = junto["casos"][0]
        self.assertEqual(login["nome"], "1. Login")
        self.assertIn({"tipo": "existe", "alvo": "data.token", "valor": ""}, login["assercoes"])
        self.assertEqual(len(login["extrair"]), 1)
        self.assertIn("1 caso(s) repetido(s)", junto["nota_app"])
        self.assertEqual(junto["observacoes"], "")

    def test_mesma_rota_com_status_diferente_nao_e_repeticao(self):
        r1 = {"casos": [_caso("GET", "{{base_url}}/api/v1/psychosocial-surveys", "200")]}
        r2 = {"casos": [_caso("GET", "{{base_url}}/api/v1/psychosocial-surveys", "403")]}
        self.assertEqual(len(lotes_ia.juntar_respostas([r1, r2])["casos"]), 2)

    def test_variaveis_sem_repetir_e_secreta_prevalece(self):
        r1 = {"variaveis": [{"nome": "password", "secreto": False, "descricao": "senha"}, {"nome": "base_url"}]}
        r2 = {"variaveis": [{"nome": "password", "secreto": True}, {"nome": "email", "secreto": False}]}
        variaveis = lotes_ia.juntar_respostas([r1, r2])["variaveis"]
        self.assertEqual([v["nome"] for v in variaveis], ["password", "email"])
        self.assertTrue(variaveis[0]["secreto"])
        self.assertEqual(variaveis[0]["descricao"], "senha")


class ContextoLotesAnterioresTest(unittest.TestCase):
    def test_primeiro_lote_sem_contexto(self):
        self.assertEqual(lotes_ia.contexto_lotes_anteriores([]), "")

    def test_lista_casos_e_variaveis_extraidas(self):
        texto = lotes_ia.contexto_lotes_anteriores([{"casos": [_caso(**LOGIN)]}])
        self.assertIn("POST {{base_url}}/api/v1/auth/login → 200 (extrai {{auth_token}})", texto)
        self.assertIn("Variáveis já disponíveis (extraídas acima): {{auth_token}}", texto)
        self.assertIn("NÃO repita", texto)

    def test_variaveis_para_ia_incluem_as_da_tela_e_as_dos_lotes(self):
        variaveis = lotes_ia.juntar_variaveis([{"nome": "email", "valor": "x@y", "secreto": False}],
                                              [{"variaveis": [{"nome": "password", "secreto": True}]}])
        self.assertEqual(variaveis, [{"nome": "email", "secreto": False}, {"nome": "password", "secreto": True}])


if __name__ == "__main__":
    unittest.main()
