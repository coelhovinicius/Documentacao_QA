import base64
import html
import xml.sax.saxutils as saxutils

import requests

API_VERSION = "7.1"


class AzureDevOpsError(Exception):
    """Erro de negócio ao falar com a API do Azure DevOps (mensagem já amigável)."""


class AzureDevOpsClient:
    """
    Cliente para criar Test Cases, Test Plans e Test Suites no Azure DevOps
    via REST API, usando um Personal Access Token (PAT).

    Documentação de referência:
    - Work Items: https://learn.microsoft.com/rest/api/azure/devops/wit/work-items
    - Test Plans: https://learn.microsoft.com/rest/api/azure/devops/testplan/test-plans
    - Test Suites: https://learn.microsoft.com/rest/api/azure/devops/testplan/test-suites
    - Add Test Cases to Suite (API clássica de Test):
      https://learn.microsoft.com/rest/api/azure/devops/test/test-cases/add-test-cases-to-suite
    """

    def __init__(self, organization: str, project: str, pat: str):
        self.organization = (organization or "").strip()
        self.project = (project or "").strip()
        self.pat = (pat or "").strip()

        token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("utf-8")
        self.headers_json = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.headers_json_patch = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json",
        }

    def _base_url(self) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_apis"

    def is_configured(self) -> bool:
        return bool(self.organization and self.project and self.pat)

    def _handle_response(self, response: requests.Response, context: str) -> dict:
        if response.status_code == 401:
            raise AzureDevOpsError(
                f"[{context}] Autenticação falhou (401). Verifique se o PAT é válido, "
                "não expirou, e tem os escopos 'Work Items (Read & Write)' e "
                "'Test Management (Read & Write)'."
            )
        if response.status_code == 403:
            raise AzureDevOpsError(
                f"[{context}] Sem permissão (403). O usuário dono do PAT precisa ter "
                "permissão de criação de Work Items e Test Plans neste projeto."
            )
        if response.status_code == 404:
            raise AzureDevOpsError(
                f"[{context}] Não encontrado (404). Confira o nome da organização e do "
                "projeto — eles diferenciam maiúsculas/minúsculas e espaços."
            )
        if not response.ok:
            preview = response.text[:500]
            raise AzureDevOpsError(
                f"[{context}] Erro {response.status_code} do Azure DevOps: {preview}"
            )
        if not response.text.strip():
            return {}
        return response.json()

    # ------------------------------------------------------------------ #
    # Test Cases (work items)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_steps_xml(passos: list) -> str:
        """
        Monta o XML esperado pelo campo Microsoft.VSTS.TCM.Steps.
        Cada ação/resultado precisa ir como HTML dentro de uma
        parameterizedString, e essa string HTML precisa estar escapada
        como texto XML (escapamento duplo).
        """
        steps_inner = ""
        for i, passo in enumerate(passos or [], start=1):
            acao_html = f"<DIV><P>{html.escape(str(passo.get('acao', '')))}</P></DIV>"
            esperado_html = f"<DIV><P>{html.escape(str(passo.get('resultado_esperado', '')))}</P></DIV>"
            steps_inner += (
                f'<step id="{i}" type="ActionStep">'
                f'<parameterizedString isformatted="true">{saxutils.escape(acao_html)}</parameterizedString>'
                f'<parameterizedString isformatted="true">{saxutils.escape(esperado_html)}</parameterizedString>'
                f"<description/>"
                f"</step>"
            )
        total = len(passos or [])
        return f'<steps id="0" last="{total}">{steps_inner}</steps>'

    def create_test_case(self, titulo: str, pre_condicoes: str, passos: list) -> int:
        """Cria um work item do tipo Test Case e retorna o ID numérico criado."""
        pre_html = f"<DIV><P><B>Pré-condições:</B> {html.escape(pre_condicoes or '')}</P></DIV>"

        body = [
            {"op": "add", "path": "/fields/System.Title", "value": titulo},
            {"op": "add", "path": "/fields/System.Description", "value": pre_html},
            {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": self._build_steps_xml(passos)},
        ]

        url = f"{self._base_url()}/wit/workitems/$Test%20Case?api-version={API_VERSION}"
        response = requests.post(url, json=body, headers=self.headers_json_patch, timeout=60)
        data = self._handle_response(response, f"Criar Test Case '{titulo}'")
        return data["id"]

    # ------------------------------------------------------------------ #
    # Test Plans
    # ------------------------------------------------------------------ #
    def create_test_plan(self, nome: str, descricao: str = "") -> dict:
        """Cria um Test Plan e retorna {'id':, 'root_suite_id':}."""
        body = {
            "name": nome,
            "areaPath": self.project,
            "iteration": self.project,
        }
        if descricao:
            body["description"] = descricao

        url = f"{self._base_url()}/testplan/plans?api-version={API_VERSION}"
        response = requests.post(url, json=body, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Criar Test Plan '{nome}'")

        root_suite = data.get("rootSuite") or {}
        return {"id": data["id"], "root_suite_id": root_suite.get("id")}

    def create_test_suite(self, plan_id: int, parent_suite_id: int, nome: str) -> int:
        """Cria uma Static Test Suite dentro de um plano e retorna o ID da suite."""
        body = {
            "suiteType": "StaticTestSuite",
            "name": nome,
            "parentSuite": {"id": parent_suite_id},
        }
        url = f"{self._base_url()}/testplan/Plans/{plan_id}/suites?api-version={API_VERSION}"
        response = requests.post(url, json=body, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Criar Suite '{nome}'")
        return data["id"]

    def add_cases_to_suite(self, plan_id: int, suite_id: int, test_case_ids: list) -> None:
        """Vincula uma lista de IDs de Test Case (já existentes) a uma suite."""
        if not test_case_ids:
            return
        ids_str = ",".join(str(i) for i in test_case_ids)
        url = (
            f"{self._base_url()}/test/Plans/{plan_id}/Suites/{suite_id}"
            f"/testcases/{ids_str}?api-version={API_VERSION}"
        )
        response = requests.post(url, headers=self.headers_json, timeout=60)
        self._handle_response(response, f"Vincular casos à suite {suite_id}")

    def work_item_url(self, work_item_id: int) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_workitems/edit/{work_item_id}"

    def test_plan_url(self, plan_id: int) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_testPlans/execute?planId={plan_id}"
