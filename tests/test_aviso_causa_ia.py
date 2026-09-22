"""A causa real da falha de IA vira mensagem em português e fica visível na tela."""
import unittest

from qa_testgen.ui.ia_retry import IaRetryMixin


class _Estado:
    def __init__(self):
        self.dados = {}

    def set(self, k, v):
        self.dados[k] = v

    def get(self, k, default=None):
        return self.dados.get(k, default)


class _Falso(IaRetryMixin):
    def __init__(self):
        self.state = _Estado()


# erros reais, como o provedor devolve (dentro do "detalhe" do 502 do n8n)
GROQ_TPM = ('Todos os provedores de IA falharam (OpenAI, Gemini, Groq, Groq 2, Mistral). Request too large for '
            'model openai/gpt-oss-120b in organization org_01kw on tokens per minute (TPM): Limit 8000, '
            'Requested 8716, please reduce your message size and try again.')
MISTRAL_429 = 'API error occurred: Status 429 Body: {"object":"error","message":"Rate limit exceeded","code":"1300"}'
GEMINI_QUOTA = 'Quota exceeded for quota metric generate_content_free_tier_requests, resource_exhausted'
CREDENCIAL = 'Incorrect API key provided: sk-***. 401 Unauthorized'
TIMEOUT = "O n8n respondeu com o corpo vazio (Status 200)."


class CausaDaFalhaTests(unittest.TestCase):
    def test_cota_por_minuto_do_groq(self):
        causa = IaRetryMixin._causa_da_falha(GROQ_TPM)
        self.assertIn("Cota de IA estourada (tokens por minuto)", causa)
        self.assertIn("tenta de novo sozinho", causa)   # diz o que o app está fazendo

    def test_rate_limit_do_mistral(self):
        self.assertIn("rate limit", IaRetryMixin._causa_da_falha(MISTRAL_429).lower())

    def test_cota_esgotada_avisa_que_esperar_nao_resolve(self):
        causa = IaRetryMixin._causa_da_falha(GEMINI_QUOTA)
        self.assertIn("esperar não", causa)

    def test_credencial_invalida(self):
        self.assertIn("Credencial", IaRetryMixin._causa_da_falha(CREDENCIAL))

    def test_sem_resposta_a_tempo(self):
        self.assertIn("não chegou a tempo", IaRetryMixin._causa_da_falha(TIMEOUT))

    def test_erro_desconhecido_nao_inventa_causa(self):
        self.assertEqual(IaRetryMixin._causa_da_falha("Unknown error [line 26]"), "")


class AvisoVisivelTests(unittest.TestCase):
    def setUp(self):
        self.ui = _Falso()

    def test_guarda_causa_resumo_e_detalhe_para_a_tela(self):
        self.ui._registrar_causa_visivel("lote 1 de 5", GROQ_TPM, resolvido=False, detalhe_extra="tentativa 1 de 3")
        aviso = self.ui.state.get('ia_causa_visivel')
        self.assertEqual(aviso["resumo"], "cota de IA por minuto estourada")
        self.assertIn("Cota de IA estourada", aviso["causa"])
        self.assertEqual(aviso["onde"], "lote 1 de 5")
        self.assertIn("Limit 8000", aviso["detalhe"])          # erro cru fica disponível no detalhe
        self.assertEqual(aviso["extra"], "tentativa 1 de 3")

    def test_erro_sem_padrao_conhecido_ainda_aparece(self):
        self.ui._registrar_causa_visivel("lote 2 de 5", "Unknown error [line 26]", resolvido=False)
        aviso = self.ui.state.get('ia_causa_visivel')
        self.assertEqual(aviso["resumo"], "falha ao chamar a IA")
        self.assertEqual(aviso["causa"], "")                   # sem chute de causa
        self.assertIn("Unknown error", aviso["detalhe"])       # mas o detalhe real continua visível

    def test_nova_geracao_limpa_o_aviso_anterior(self):
        self.ui._registrar_causa_visivel("lote 1 de 5", GROQ_TPM, resolvido=False)
        self.ui.state.delete = lambda k: None
        self.ui.trigger_action = lambda a: None
        self.ui._iniciar_geracao_em_lotes("gerar", "geracao_planos")
        self.assertIsNone(self.ui.state.get('ia_causa_visivel'))


if __name__ == "__main__":
    unittest.main()
