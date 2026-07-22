import base64
import html
import xml.sax.saxutils as saxutils

import requests
from urllib3.util.retry import Retry

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

        # Reaproveita conexões TCP/TLS entre chamadas (bem mais rápido que
        # abrir uma conexão nova a cada requisição). pool_maxsize aumentado
        # porque o app dispara várias chamadas em paralelo (ThreadPoolExecutor).
        # Retry automático: a maioria dos "Connection aborted"/reset é
        # transitória (rede local, throttling do lado do Azure DevOps) e
        # passa numa segunda tentativa, sem precisar que o usuário reenvie.
        retry_strategy = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.8,  # 0.8s, 1.6s, 3.2s, 6.4s entre tentativas
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PATCH"],
            raise_on_status=False,
        )
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=retry_strategy
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _base_url(self) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_apis"

    def is_configured(self) -> bool:
        return bool(self.organization and self.project and self.pat)

    def is_org_pat_configured(self) -> bool:
        """Só checa Organização + PAT — usado para listar Projetos, que não
        depende de um Projeto já estar escolhido."""
        return bool(self.organization and self.pat)

    def list_projects(self) -> list:
        """
        Lista os nomes dos projetos da organização que ESTE PAT consegue
        visualizar (a API do Azure DevOps já filtra pelas permissões do
        usuário/token — não é preciso filtrar manualmente).
        """
        names = []
        url = f"https://dev.azure.com/{self.organization}/_apis/projects?api-version={API_VERSION}&$top=1000"
        while url:
            response = self.session.get(url, headers=self.headers_json, timeout=60)
            data = self._handle_response(response, "Listar Projetos da Organização")
            for p in data.get("value", []):
                name = p.get("name")
                if name:
                    names.append(name)
            continuation = response.headers.get("x-ms-continuationtoken")
            if continuation:
                url = (
                    f"https://dev.azure.com/{self.organization}/_apis/projects"
                    f"?continuationToken={continuation}&api-version={API_VERSION}&$top=1000"
                )
            else:
                url = None
        return sorted(names, key=str.lower)

    def list_area_paths(self) -> list:
        """
        Lista os Area Paths REAIS já existentes no projeto atual (self.project),
        varrendo a árvore de "classification nodes" do Azure DevOps. Retorna
        os caminhos completos (ex.: "QA-TestGen-Sandbox", "QA-TestGen-Sandbox\\Time A"),
        exatamente como o Azure DevOps espera no campo Area Path.
        """
        url = f"{self._base_url()}/wit/classificationnodes/Areas?$depth=50&api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, "Listar Area Paths do Projeto")

        paths = []

        def _walk(node, prefix):
            name = node.get("name", "")
            full = f"{prefix}\\{name}" if prefix else name
            if full:
                paths.append(full)
            for child in node.get("children") or []:
                _walk(child, full)

        _walk(data, "")
        return paths

    def list_accessible_organizations(self) -> list:
        """
        Lista as organizações do Azure DevOps que este PAT consegue acessar
        (informativo). Usa um host diferente (vssps.visualstudio.com),
        próprio da API de perfil/contas do Azure DevOps.
        """
        profile_url = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1"
        resp = self.session.get(profile_url, headers=self.headers_json, timeout=30)
        data = self._handle_response(resp, "Buscar perfil do usuário do PAT")
        member_id = data.get("id")
        if not member_id:
            return []

        accounts_url = f"https://app.vssps.visualstudio.com/_apis/accounts?memberId={member_id}&api-version=7.1"
        resp2 = self.session.get(accounts_url, headers=self.headers_json, timeout=30)
        data2 = self._handle_response(resp2, "Listar organizações acessíveis")
        names = [a.get("accountName") for a in data2.get("value", []) if a.get("accountName")]
        return sorted(names, key=str.lower)

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

    # Reference name do campo customizado "Pre condicoes" no processo de
    # Test Case dessa organização. Se reusar esse cliente em outra
    # organização/projeto com um processo diferente, confira o nome certo
    # rodando list_test_case_fields.py e ajuste aqui.
    PRECONDICOES_FIELD = "Custom.Precondicoes"

    def create_test_case(self, titulo: str, pre_condicoes: str, passos: list, area_path: str = None, initial_state: str = None) -> dict:
        """
        Cria um work item do tipo Test Case e retorna {'id': int, 'state_warning': str|None}.

        O campo System.State não pode ser definido direto na criação — o
        Azure DevOps valida isso como uma transição de workflow (ex.: só
        permite ir de "Design" para "Ready" via uma atualização posterior,
        não no mesmo PATCH que cria o item). Por isso, criamos primeiro no
        estado padrão (garantido funcionar) e, se um `initial_state`
        diferente foi pedido, tentamos mudar depois — sem derrubar a criação
        do caso se essa segunda etapa falhar.
        """
        body = [
            {"op": "add", "path": "/fields/System.Title", "value": titulo},
            {"op": "add", "path": f"/fields/{self.PRECONDICOES_FIELD}", "value": pre_condicoes or ""},
            {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": self._build_steps_xml(passos)},
        ]
        if area_path:
            body.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})

        url = f"{self._base_url()}/wit/workitems/$Test%20Case?api-version={API_VERSION}"
        response = self.session.post(url, json=body, headers=self.headers_json_patch, timeout=60)
        data = self._handle_response(response, f"Criar Test Case '{titulo}'")
        work_item_id = data["id"]

        state_warning = None
        if initial_state:
            try:
                self.set_work_item_state(work_item_id, initial_state)
            except AzureDevOpsError as error:
                state_warning = (
                    f"Caso criado, mas não foi possível mudar o estado para '{initial_state}' "
                    f"(ficou no estado padrão): {error}"
                )

        return {"id": work_item_id, "state_warning": state_warning}

    def set_work_item_state(self, work_item_id: int, state: str) -> None:
        """Atualiza o campo System.State de um Work Item já existente."""
        body = [{"op": "add", "path": "/fields/System.State", "value": state}]
        url = f"{self._base_url()}/wit/workitems/{work_item_id}?api-version={API_VERSION}"
        response = self.session.patch(url, json=body, headers=self.headers_json_patch, timeout=60)
        self._handle_response(response, f"Mudar estado do Work Item {work_item_id} para '{state}'")

    # ------------------------------------------------------------------ #
    # Test Plans
    # ------------------------------------------------------------------ #
    def list_test_plan_names(self) -> list:
        """
        Lista os nomes de todos os Test Plans já existentes no projeto —
        usado para checar duplicidade antes de criar um novo.
        """
        names = []
        url = f"{self._base_url()}/testplan/plans?api-version={API_VERSION}"
        while url:
            response = self.session.get(url, headers=self.headers_json, timeout=60)
            data = self._handle_response(response, "Listar Test Plans existentes")
            for plan in data.get("value", []):
                name = plan.get("name")
                if name:
                    names.append(name)
            continuation = response.headers.get("x-ms-continuationtoken")
            if continuation:
                url = f"{self._base_url()}/testplan/plans?continuationToken={continuation}&api-version={API_VERSION}"
            else:
                url = None
        return names

    def test_plan_name_exists(self, name: str) -> bool:
        """Checagem case-insensitive de nome duplicado de Test Plan no projeto."""
        target = (name or "").strip().lower()
        if not target:
            return False
        return any(existing.strip().lower() == target for existing in self.list_test_plan_names())

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
        response = self.session.post(url, json=body, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Criar Test Plan '{nome}'")

        root_suite = data.get("rootSuite") or {}
        return {"id": data["id"], "root_suite_id": root_suite.get("id")}

    def create_requirement_based_suite(self, plan_id: int, parent_suite_id: int, work_item_id: int) -> int:
        """
        Cria uma Requirement-based Suite dentro do plano, vinculada ao Work
        Item informado. O Azure DevOps nomeia a suite automaticamente com o
        título do Work Item, e ela passa a "puxar" sozinha qualquer Test Case
        que tenha um link 'Tests' apontando pra esse Work Item.
        """
        body = {
            "suiteType": "RequirementTestSuite",
            "requirementId": work_item_id,
            "parentSuite": {"id": parent_suite_id},
        }
        url = f"{self._base_url()}/testplan/Plans/{plan_id}/suites?api-version={API_VERSION}"
        response = self.session.post(url, json=body, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Criar Requirement Suite para Work Item {work_item_id}")
        return data["id"]

    def create_test_suite(self, plan_id: int, parent_suite_id: int, nome: str) -> int:
        """Cria uma Static Test Suite dentro de um plano e retorna o ID da suite."""
        body = {
            "suiteType": "StaticTestSuite",
            "name": nome,
            "parentSuite": {"id": parent_suite_id},
        }
        url = f"{self._base_url()}/testplan/Plans/{plan_id}/suites?api-version={API_VERSION}"
        response = self.session.post(url, json=body, headers=self.headers_json, timeout=60)
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
        response = self.session.post(url, headers=self.headers_json, timeout=60)
        self._handle_response(response, f"Vincular casos à suite {suite_id}")

    def work_item_url(self, work_item_id: int) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_workitems/edit/{work_item_id}"

    def test_plan_url(self, plan_id: int) -> str:
        return f"https://dev.azure.com/{self.organization}/{self.project}/_testPlans/execute?planId={plan_id}"

    # ------------------------------------------------------------------ #
    # Work Items existentes (para vincular Test Cases a eles)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _wiql_escape(value: str) -> str:
        """Escapa aspas simples para uso dentro de uma string WIQL."""
        return (value or "").replace("'", "''")

    # Tipos de work item que NUNCA são "requisitos" pra vincular casos de
    # teste — são artefatos do próprio Test Plans (inclusive criados pela
    # integração), não itens reais do backlog.
    EXCLUDED_TYPES = {
        "Test Case", "Test Plan", "Test Suite",
        "Epic", "Task", "Spike", "Feature", "Improvement",
    }

    # Estados que NUNCA devem entrar na integração — nem como sugestão, nem
    # manualmente. Ajuste essa lista se o processo do seu projeto usar outros
    # nomes de estado (ex.: "Done", "Closed", "Removed").
    EXCLUDED_STATES = {"Finalizado", "Backlog"}

    def fetch_work_items_by_area_path(self, area_path: str) -> list:
        """
        Busca (via WIQL) todos os Work Items dentro do Area Path informado,
        exceto os tipos em EXCLUDED_TYPES e os estados em EXCLUDED_STATES.
        Retorna uma lista de dicts: {'id': int, 'title': str, 'type': str, 'state': str}
        """
        project_esc = self._wiql_escape(self.project)
        area_esc = self._wiql_escape(area_path or self.project)

        state_filters = " ".join(
            f"AND [System.State] <> '{self._wiql_escape(s)}'" for s in self.EXCLUDED_STATES
        )
        type_filters = " ".join(
            f"AND [System.WorkItemType] <> '{self._wiql_escape(t)}'" for t in self.EXCLUDED_TYPES
        )

        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = '{project_esc}' "
                f"AND [System.AreaPath] UNDER '{area_esc}' "
                f"{type_filters} "
                f"{state_filters} "
                "ORDER BY [System.Id]"
            )
        }
        url = f"{self._base_url()}/wit/wiql?api-version={API_VERSION}"
        response = self.session.post(url, json=wiql, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, "Buscar Work Items por Area Path")

        ids = [str(wi["id"]) for wi in data.get("workItems", [])]
        if not ids:
            return []

        ids_str = ",".join(ids)
        fields = "System.Id,System.Title,System.WorkItemType,System.State"
        details_url = (
            f"{self._base_url()}/wit/workitems?ids={ids_str}&fields={fields}"
            f"&api-version={API_VERSION}"
        )
        details_response = self.session.get(details_url, headers=self.headers_json, timeout=60)
        details_data = self._handle_response(details_response, "Detalhar Work Items")

        items = []
        for wi in details_data.get("value", []):
            f = wi.get("fields", {})
            state = f.get("System.State", "")
            wi_type = f.get("System.WorkItemType", "")
            if state in self.EXCLUDED_STATES or wi_type in self.EXCLUDED_TYPES:
                # Rede de segurança: mesmo que o filtro do WIQL falhe por
                # algum motivo (nome de campo customizado, cache, etc.),
                # ainda garante que esses itens nunca aparecem na lista.
                continue
            items.append({
                "id": wi["id"],
                "title": f.get("System.Title", ""),
                "type": f.get("System.WorkItemType", ""),
                "state": state,
            })
        return items

    def link_test_case_to_work_item(
        self, test_case_id: int, work_item_id: int, comment: str = "Vinculado via QA TestGen"
    ) -> None:
        """
        Cria um vínculo do tipo 'Tests' no Test Case, apontando para o Work
        Item (ex.: User Story, Feature, Bug). Esse é exatamente o link que uma
        Requirement-based Suite usa para "puxar" automaticamente os casos de
        teste vinculados ao Work Item selecionado.
        """
        target_url = f"{self._base_url()}/wit/workItems/{work_item_id}"
        body = [{
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "Microsoft.VSTS.Common.TestedBy-Reverse",
                "url": target_url,
                "attributes": {"comment": comment},
            },
        }]
        url = f"{self._base_url()}/wit/workitems/{test_case_id}?api-version={API_VERSION}"
        response = self.session.patch(url, json=body, headers=self.headers_json_patch, timeout=60)
        self._handle_response(response, f"Vincular Test Case {test_case_id} ao Work Item {work_item_id}")
