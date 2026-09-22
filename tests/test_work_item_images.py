"""Coleta de imagens de um Work Item (anexos, <img> embutidas e Casos de Teste vinculados)."""
import unittest

from qa_testgen.infrastructure.azure_devops_client import AzureDevOpsClient

STEPS_XML = (
    '<steps id="0" last="2">'
    '<step id="2" type="ActionStep"><parameterizedString isformatted="true">'
    '&lt;DIV&gt;Selecionar o motivo &lt;img src="https://dev.azure.com/org/_apis/wit/attachments/aaa?fileName=motivo.png"&gt;&lt;/DIV&gt;'
    '</parameterizedString><parameterizedString isformatted="true">Motivo destacado</parameterizedString></step>'
    '<step id="3" type="ActionStep"><parameterizedString isformatted="true">Clicar em prosseguir</parameterizedString>'
    '<parameterizedString isformatted="true">Tela muda</parameterizedString></step>'
    '</steps>'
)

US = {  # User Story: sem anexo próprio, só o Caso de Teste vinculado
    "fields": {"System.Title": "Persistir motivo", "System.WorkItemType": "User Story",
               "System.Description": '<p>Fluxo de cancelamento <img src="https://dev.azure.com/org/_apis/wit/attachments/desc?fileName=tela.png"></p>'},
    "relations": [{"rel": "Microsoft.VSTS.Common.TestedBy-Forward", "url": "https://dev.azure.com/org/_apis/wit/workItems/999"}],
}
CASO = {
    "fields": {"System.Title": "CT01 - Cancelamento", "System.WorkItemType": "Test Case",
               "Custom.Precondicoes": "<p>Usuário com plano ativo</p>", "Microsoft.VSTS.TCM.Steps": STEPS_XML},
    "relations": [
        {"rel": "AttachedFile", "url": "https://dev.azure.com/org/_apis/wit/attachments/p3",
         "attributes": {"name": "evidencia.png", "comment": "[TestStep=3]: prova do passo"}},
        {"rel": "AttachedFile", "url": "https://dev.azure.com/org/_apis/wit/attachments/outro",
         "attributes": {"name": "evidencia.png", "comment": "anexo geral"}},
        {"rel": "AttachedFile", "url": "https://dev.azure.com/org/_apis/wit/attachments/doc",
         "attributes": {"name": "especificacao.docx"}},
    ],
}


class _Resp:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload, self.content, self.status_code = payload, content, status
        self.text, self.ok, self.headers = "ok", status < 400, {}

    def json(self):
        return self._payload


class _Session:
    """Responde os GETs do client: work item por id e download de anexo/imagem."""

    def __init__(self):
        self.baixados = []

    def get(self, url, **kwargs):
        if "/wit/workitems/999" in url:
            return _Resp(CASO)
        if "/wit/workitems/1" in url:
            return _Resp(US)
        self.baixados.append(url)
        return _Resp(content=b"PNG" + url.encode()[-6:])


class WorkItemImagesTests(unittest.TestCase):
    def setUp(self):
        self.client = AzureDevOpsClient("org", "proj", "pat")
        self.client.session = _Session()

    def test_collects_from_linked_test_case_steps_and_attachments(self):
        imagens, casos = self.client.get_work_item_images(1)
        nomes = [i["filename"] for i in imagens]

        # imagem embutida na Descrição da própria US (nome vem do fileName da URL)
        self.assertIn("WI1_tela.png", nomes)
        # imagem embutida no passo do Caso de Teste vinculado
        self.assertIn("WI999_motivo.png", nomes)
        # anexo .docx é ignorado; os dois .png de mesmo nome não se sobrescrevem
        self.assertNotIn("WI999_especificacao.docx", nomes)
        self.assertEqual(len([n for n in nomes if n.startswith("WI999_evidencia")]), 2)
        self.assertEqual(len(nomes), len(set(nomes)), f"nomes repetidos: {nomes}")

        # cada imagem chega com o texto ao redor — é o que a IA usa pra achar o passo
        por_nome = {i["filename"]: i for i in imagens}
        self.assertIn("Selecionar o motivo", por_nome["WI999_motivo.png"]["context"])
        # [TestStep=3] é o ID interno do step (o 1o step do XML tem id=2), ou seja,
        # o 2o passo que a pessoa vê — casar pela posição jogava a foto no passo errado
        anexo_de_passo = next(i for i in imagens if "anexo do passo" in i["context"])
        self.assertIn("anexo do passo 2: Clicar em prosseguir", anexo_de_passo["context"])
        self.assertIn("Tela muda", anexo_de_passo["context"])
        self.assertIn("Fluxo de cancelamento", por_nome["WI1_tela.png"]["context"])

        # o Caso vinculado volta com pré-condições e passos, pro conteúdo do manual
        self.assertEqual(len(casos), 1)
        self.assertEqual(casos[0]["id"], 999)
        self.assertEqual(casos[0]["pre_condicoes"], "Usuário com plano ativo")
        self.assertEqual([p["numero"] for p in casos[0]["passos"]], [1, 2])
        self.assertIn("Selecionar o motivo", casos[0]["passos"][0]["acao"])

    def test_same_attachment_is_downloaded_once(self):
        imagens, _ = self.client.get_work_item_images(1)
        self.assertEqual(len(self.client.session.baixados), len(imagens))
        self.assertEqual(len(set(self.client.session.baixados)), len(self.client.session.baixados))

    def test_can_skip_linked_items(self):
        imagens, casos = self.client.get_work_item_images(1, incluir_vinculados=False)
        self.assertEqual([i["filename"] for i in imagens], ["WI1_tela.png"])
        self.assertEqual(casos, [])


if __name__ == "__main__":
    unittest.main()
