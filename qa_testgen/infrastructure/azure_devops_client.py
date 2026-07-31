import base64
import html
import re
import unicodedata
import xml.sax.saxutils as saxutils
from urllib.parse import quote

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
        return f"https://dev.azure.com/{quote(self.organization, safe='')}/{quote(self.project, safe='')}/_apis"

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
        url = f"https://dev.azure.com/{quote(self.organization, safe='')}/_apis/projects?api-version={API_VERSION}&$top=1000"
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
                    f"https://dev.azure.com/{quote(self.organization, safe='')}/_apis/projects"
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
    def list_test_plans(self) -> list:
        """Lista os Test Plans do projeto como [{'id':..., 'name':..., 'area_path':...}]."""
        plans = []
        url = f"{self._base_url()}/testplan/plans?api-version={API_VERSION}"
        while url:
            response = self.session.get(url, headers=self.headers_json, timeout=60)
            data = self._handle_response(response, "Listar Test Plans existentes")
            for plan in data.get("value", []):
                if plan.get("name"):
                    plans.append({
                        "id": plan.get("id"),
                        "name": plan.get("name"),
                        "area_path": plan.get("areaPath", ""),
                    })
            continuation = response.headers.get("x-ms-continuationtoken")
            if continuation:
                url = f"{self._base_url()}/testplan/plans?continuationToken={continuation}&api-version={API_VERSION}"
            else:
                url = None
        return plans

    def test_plan_name_exists(self, name: str) -> bool:
        """Checagem case-insensitive de nome duplicado de Test Plan no projeto."""
        target = (name or "").strip().lower()
        if not target:
            return False
        return any(p["name"].strip().lower() == target for p in self.list_test_plans())

    def list_test_plans_for_area_path(self, area_path: str) -> list:
        """Mesma coisa que list_test_plans(), mas só os que pertencem à Area Path informada."""
        return [p for p in self.list_test_plans() if p.get("area_path") == area_path]

    def get_test_plan_root_suite(self, plan_id: int) -> int:
        """
        Busca o ID da suite raiz de um Test Plan JÁ EXISTENTE (a criação de
        um plano novo já devolve isso direto na resposta, mas pra um plano
        existente que a pessoa escolheu reaproveitar, precisa buscar
        separadamente).
        """
        url = f"{self._base_url()}/testplan/plans/{plan_id}?api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Buscar detalhes do Test Plan {plan_id}")
        root_suite = data.get("rootSuite") or {}
        return root_suite.get("id")

    def get_existing_requirement_suite_ids(self, plan_id: int) -> dict:
        """
        Mapeia work_item_id -> suite_id das Requirement-based Suites que já
        existem num Test Plan — usado pra não duplicar suite quando a
        pessoa escolhe reaproveitar um Test Plan já existente (regra do
        "merge": só cria suite pros Work Items que ainda não têm uma).
        """
        mapping = {}
        url = f"{self._base_url()}/testplan/Plans/{plan_id}/suites?api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Listar Suites existentes do Test Plan {plan_id}")
        for suite in data.get("value", []):
            requirement_id = suite.get("requirementId")
            if requirement_id:
                mapping[int(requirement_id)] = suite.get("id")
        return mapping

    # ------------------------------------------------------------------ #
    # Queries WIQL — geração assistida por IA (descrição em linguagem
    # natural → WIQL → confirmação → query salva no Azure DevOps).
    # ------------------------------------------------------------------ #
    def run_wiql_query(self, wiql: str) -> dict:
        """
        Executa uma query WIQL como TESTE (não salva nada) — usado pra
        mostrar um preview de quantos/quais Work Items a query traria,
        antes da pessoa confirmar e salvar de verdade.

        Retorna {"count": int, "items": [{"id": int, "url": str}, ...]}.
        Uma query mal formada faz essa chamada retornar erro HTTP 400 —
        nesse caso, é a própria API do Azure DevOps que aponta o problema
        de sintaxe, então a mensagem de erro tende a ser útil.
        """
        url = f"{self._base_url()}/wit/wiql?api-version={API_VERSION}"
        response = self.session.post(url, headers=self.headers_json, json={"query": wiql}, timeout=60)
        data = self._handle_response(response, "Executar query WIQL (teste)")
        items = [
            {"id": wi.get("id"), "url": wi.get("url", "")}
            for wi in data.get("workItems", [])
            if wi.get("id")
        ]
        return {"count": len(items), "items": items}

    def get_work_items_basic_fields(self, ids: list) -> list:
        """
        Busca ID, Título, Tipo e Estado de uma lista de Work Items — usado
        pra montar a tabela de preview da query WIQL (dados de verdade,
        não só a contagem), sem salvar nada no Azure DevOps.
        """
        if not ids:
            return []
        results = []
        # A API do Azure DevOps limita a 200 IDs por chamada de batch.
        for i in range(0, len(ids), 200):
            batch = ids[i:i + 200]
            ids_str = ",".join(str(x) for x in batch)
            fields = "System.Id,System.Title,System.WorkItemType,System.State"
            url = f"{self._base_url()}/wit/workitems?ids={ids_str}&fields={fields}&api-version={API_VERSION}"
            response = self.session.get(url, headers=self.headers_json, timeout=60)
            data = self._handle_response(response, "Buscar dados dos Work Items retornados pela query")
            for wi in data.get("value", []):
                f = wi.get("fields", {})
                results.append({
                    "id": wi.get("id"),
                    "title": f.get("System.Title", ""),
                    "type": f.get("System.WorkItemType", ""),
                    "state": f.get("System.State", ""),
                })
        return results

    def create_shared_query(self, name: str, wiql: str, folder: str = "My Queries") -> dict:
        """
        Cria a query de verdade no Azure DevOps, dentro da pasta indicada
        (por padrão 'My Queries' — pessoal, sempre permitido; 'Shared
        Queries' requer permissão de escrita na pasta compartilhada do
        projeto, nem toda conta tem).

        Retorna {"id": str, "url": str} da query criada.
        """
        folder_encoded = quote(folder, safe='')
        url = f"{self._base_url()}/wit/queries/{folder_encoded}?api-version={API_VERSION}"
        body = {"name": name, "wiql": wiql, "isFolder": False}
        response = self.session.post(url, headers=self.headers_json, json=body, timeout=60)
        data = self._handle_response(response, f"Criar query '{name}' em '{folder}'")
        return {"id": data.get("id", ""), "url": data.get("_links", {}).get("html", {}).get("href", "")}

    def get_profile_display_name(self) -> str:
        """
        Retorna o nome de exibição do dono deste PAT, via API de perfil do
        Azure DevOps (mesmo endpoint usado para listar organizações
        acessíveis). Usado para preencher "gerado por" automaticamente nos
        relatórios PDF, sem precisar digitar.
        """
        url = "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.1"
        response = self.session.get(url, headers=self.headers_json, timeout=30)
        data = self._handle_response(response, "Buscar perfil do usuário do PAT")
        return data.get("displayName", "") or ""

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
        return (
            f"https://dev.azure.com/{quote(self.organization, safe='')}"
            f"/{quote(self.project, safe='')}/_workitems/edit/{work_item_id}"
        )

    def test_plan_url(self, plan_id: int) -> str:
        return (
            f"https://dev.azure.com/{quote(self.organization, safe='')}"
            f"/{quote(self.project, safe='')}/_testPlans/execute?planId={plan_id}"
        )

    # ------------------------------------------------------------------ #
    # Resultados de execução e evidências (Relatório de Testes)
    #
    # ATENÇÃO: essa é uma área da API do Azure DevOps que usamos bem menos
    # que a de Work Items/Test Cases — não tive como validar contra uma
    # instância real. A lógica segue a documentação pública da API de Test
    # Plans/Test Points/Test Runs, mas os nomes de campo variam um pouco
    # entre versões/processos — por isso tudo aqui é defensivo (tenta vários
    # nomes de campo possíveis, nunca derruba o relatório inteiro se um
    # caso específico vier em formato inesperado, só pula ele e registra
    # um aviso).
    # ------------------------------------------------------------------ #
    def get_test_plan_execution_summary(self, plan_id: int) -> dict:
        """
        Varre todas as Suites de um Test Plan e retorna, por Caso de Teste:
        o último resultado de execução (outcome), a Suíte a que pertence, e
        a referência de run/result (usada depois para buscar anexos/evidências).

        Retorno:
        {
            "points": [
                {"case_id": int, "case_title": str, "outcome": str,
                 "suite_name": str, "run_id": int|None, "result_id": int|None},
                ...
            ],
            "warnings": [str, ...],   # avisos de itens pulados por formato inesperado
        }
        """
        points = []
        warnings = []

        suites_url = f"{self._base_url()}/testplan/Plans/{plan_id}/suites?api-version={API_VERSION}"
        response = self.session.get(suites_url, headers=self.headers_json, timeout=60)
        suites_data = self._handle_response(response, f"Listar Suites do Test Plan {plan_id}")
        suites = [
            {"id": s.get("id"), "name": s.get("name", f"Suíte {s.get('id')}")}
            for s in suites_data.get("value", []) if s.get("id")
        ]

        for suite in suites:
            suite_id, suite_name = suite["id"], suite["name"]
            tp_url = (
                f"{self._base_url()}/testplan/Plans/{plan_id}/Suites/{suite_id}"
                f"/TestPoint?api-version={API_VERSION}"
            )
            try:
                tp_response = self.session.get(tp_url, headers=self.headers_json, timeout=60)
                tp_data = self._handle_response(tp_response, f"Listar Test Points da Suite {suite_id}")
            except AzureDevOpsError as error:
                warnings.append(f"Não foi possível ler pontos de teste da Suite {suite_id}: {error}")
                continue

            for point in tp_data.get("value", []):
                try:
                    case_ref = point.get("testCaseReference") or point.get("testCase") or {}
                    case_id = case_ref.get("id") or (point.get("testCase") or {}).get("id")
                    case_title = case_ref.get("name") or (point.get("testCase") or {}).get("name") or ""

                    results = point.get("results") or {}
                    outcome = (
                        results.get("outcome")
                        or point.get("outcome")
                        or "Not Run"
                    )
                    run_id = (
                        results.get("lastTestRunId")
                        or point.get("lastTestRunId")
                        or results.get("runId")
                    )
                    result_id = (
                        results.get("lastResultId")
                        or point.get("lastResultId")
                        or results.get("resultId")
                    )

                    if case_id is not None:
                        points.append({
                            "case_id": int(case_id),
                            "case_title": case_title,
                            "outcome": outcome,
                            "suite_name": suite_name,
                            "run_id": int(run_id) if run_id else None,
                            "result_id": int(result_id) if result_id else None,
                        })
                except Exception as error:
                    warnings.append(f"Ponto de teste em formato inesperado na Suite {suite_id}, pulado: {error}")

        return {"points": points, "warnings": warnings}

    def get_test_case_attachments(self, case_id: int) -> tuple:
        """
        Busca as imagens anexadas ao Work Item do Caso de Teste (anexos
        "de verdade" do work item, associados aos Steps — não tags <img>
        embutidas em texto). Filtra só .png/.jpg/.jpeg, ignorando outros
        tipos de anexo que porventura existam no mesmo Work Item.

        Retorna (imagens, avisos):
          imagens: [(nome_arquivo, bytes), ...]
          avisos: [str, ...] — diagnóstico do que aconteceu em cada etapa.
        """
        warnings = []
        url = f"{self._base_url()}/wit/workitems/{case_id}?$expand=relations&api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Buscar anexos do Caso de Teste {case_id}")
        relations = data.get("relations", []) or []

        attachment_rels = [r for r in relations if r.get("rel") == "AttachedFile"]
        if not attachment_rels:
            warnings.append(f"Caso {case_id}: nenhum anexo encontrado no Work Item.")
            return [], warnings

        image_exts = (".png", ".jpg", ".jpeg")
        images = []
        for rel in attachment_rels:
            attrs = rel.get("attributes", {}) or {}
            filename = attrs.get("name", "") or f"evidencia_{case_id}"
            if not filename.lower().endswith(image_exts):
                continue  # anexo de outro tipo (ex.: .docx, .pdf) — ignora

            att_url = rel.get("url", "")
            if not att_url:
                continue
            try:
                img_response = self.session.get(att_url, headers=self.headers_json, timeout=60)
                if img_response.status_code == 200 and img_response.content:
                    images.append((filename, img_response.content))
                else:
                    warnings.append(
                        f"Caso {case_id}: falha ao baixar anexo '{filename}' — "
                        f"HTTP {img_response.status_code}."
                    )
            except Exception as error:
                warnings.append(f"Caso {case_id}: erro ao baixar anexo '{filename}': {error}")

        if not images and attachment_rels:
            warnings.append(
                f"Caso {case_id}: {len(attachment_rels)} anexo(s) encontrados no Work Item, "
                "mas nenhum era .png/.jpg/.jpeg."
            )

        return images, warnings

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

    @staticmethod
    def _strip_html(raw_html: str) -> str:
        """Remove tags HTML e decodifica entidades — os campos de Descrição
        e Critérios de Aceite do Azure DevOps vêm como HTML."""
        if not raw_html:
            return ""
        text = re.sub(r'<br\s*/?>', '\n', raw_html)
        text = re.sub(r'</p>', '\n', text)
        text = re.sub(r'<li[^>]*>', '- ', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text)
        return "\n".join(line.strip() for line in text.split("\n")).strip()

    def get_work_items_full_details(self, ids: list) -> list:
        """
        Busca Descrição e Critérios de Aceite completos de uma lista
        específica de Work Items (não é chamado pra o board inteiro, só
        pros que a pessoa selecionou) — usado para gerar a especificação
        funcional a partir de Work Items, no lugar de um documento
        enviado. Campos de origem variam por tipo de processo/template;
        busca os mais comuns e ignora graciosamente o que não existir.
        """
        if not ids:
            return []
        ids_str = ",".join(str(i) for i in ids)
        fields = (
            "System.Id,System.Title,System.WorkItemType,System.Description,"
            "Microsoft.VSTS.Common.AcceptanceCriteria"
        )
        url = (
            f"{self._base_url()}/wit/workitems?ids={ids_str}&fields={fields}"
            f"&api-version={API_VERSION}"
        )
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, "Buscar detalhes completos dos Work Items selecionados")

        results = []
        for wi in data.get("value", []):
            f = wi.get("fields", {})
            results.append({
                "id": wi.get("id"),
                "title": f.get("System.Title", ""),
                "type": f.get("System.WorkItemType", ""),
                "description": self._strip_html(f.get("System.Description", "") or ""),
                "acceptance_criteria": self._strip_html(f.get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""),
            })
        return results

    def get_existing_test_case_titles(self, work_item_id: int) -> list:
        """
        Retorna os títulos dos Test Cases JÁ vinculados a esse Work Item no
        Azure DevOps (relação 'Tested By', o inverso do que
        link_test_case_to_work_item cria a partir do Test Case). Usado para
        evitar sugerir/criar Casos de Teste duplicados de algo que já existe.
        """
        url = f"{self._base_url()}/wit/workitems/{work_item_id}?$expand=relations&api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Buscar relações do Work Item {work_item_id}")

        case_ids = []
        for rel in data.get("relations") or []:
            if rel.get("rel") == "Microsoft.VSTS.Common.TestedBy-Forward":
                url_ref = rel.get("url", "")
                try:
                    case_ids.append(int(url_ref.rstrip("/").split("/")[-1]))
                except (ValueError, IndexError):
                    continue

        if not case_ids:
            return []

        ids_param = ",".join(str(c) for c in case_ids)
        url2 = f"{self._base_url()}/wit/workitems?ids={ids_param}&fields=System.Title&api-version={API_VERSION}"
        response2 = self.session.get(url2, headers=self.headers_json, timeout=60)
        data2 = self._handle_response(response2, f"Buscar títulos dos Test Cases existentes do Work Item {work_item_id}")
        return [
            wi.get("fields", {}).get("System.Title", "")
            for wi in data2.get("value", [])
            if wi.get("fields", {}).get("System.Title")
        ]

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

    # ------------------------------------------------------------------ #
    # Status de QA por coluna do board (Kanban) — usado no Relatório de
    # Testes, no lugar do outcome de execução do Test Point (que nem
    # sempre reflete a realidade de como o time realmente trabalha).
    # ------------------------------------------------------------------ #
    _COLUNAS_APROVADO = {
        "pronto para uat", "teste uat", "aguardando cab",
        "aguardando subida em producao", "testes em producao", "finalizado",
    }
    _COLUNAS_CANCELADO = {"cancelados"}
    _COLUNAS_PENDENTE = {
        "backlog", "em refinamento de negocios", "pronto para refinamento tecnico",
        "em refinamento tecnico", "pronto para validacao de produtos",
        "em validacao de produtos", "pronto para dev", "em desenvolvimento",
        "pronto para code review", "code review", "pronto para qa", "teste qa",
    }

    @staticmethod
    def _normalize(text: str) -> str:
        """minusculo, sem acento — pra comparar nome de coluna sem depender de acentuação exata."""
        if not text:
            return ""
        nfkd = unicodedata.normalize('NFKD', text)
        sem_acento = ''.join(c for c in nfkd if not unicodedata.combining(c))
        return sem_acento.strip().lower()

    @classmethod
    def _classify_board_column(cls, board_column: str):
        """Retorna 'Aprovado'/'Cancelado'/'Pendente'/None (coluna não reconhecida)."""
        norm = cls._normalize(board_column)
        if norm in cls._COLUNAS_APROVADO:
            return "Aprovado"
        if norm in cls._COLUNAS_CANCELADO:
            return "Cancelado"
        if norm in cls._COLUNAS_PENDENTE:
            return "Pendente"
        return None

    @staticmethod
    def _classify_description_text(description: str):
        """Fallback: procura frases explícitas na descrição (regra 4)."""
        norm = (description or "").lower()
        if "aprovado em homologação" in norm or "aprovado em homologacao" in norm or "aprovado em produção" in norm or "aprovado em producao" in norm:
            return "Aprovado"
        if "reprovado em homologação" in norm or "reprovado em homologacao" in norm or "reprovado em produção" in norm or "reprovado em producao" in norm:
            return "Reprovado"
        return None

    def get_tested_work_item_ids(self, test_case_id: int) -> list:
        """
        A partir de um Caso de Teste, acha os Work Items que ele testa
        (relação 'TestedBy-Reverse' — o inverso do que
        link_test_case_to_work_item cria).
        """
        url = f"{self._base_url()}/wit/workitems/{test_case_id}?$expand=relations&api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Buscar requisitos testados pelo Caso de Teste {test_case_id}")

        ids = []
        for rel in data.get("relations") or []:
            if rel.get("rel") == "Microsoft.VSTS.Common.TestedBy-Reverse":
                try:
                    ids.append(int(rel.get("url", "").rstrip("/").split("/")[-1]))
                except (ValueError, IndexError):
                    continue
        return ids

    def get_work_item_qa_status(self, work_item_id: int) -> str:
        """
        Classifica o status de QA de um Work Item pela coluna do board
        (Kanban), olhando o item e seus filhos diretos — regras 1-4 que
        você descreveu. Prioridade quando item e filhos divergem:
        Cancelado > Reprovado > Aprovado > Pendente > Desconhecido (o mais
        "definitivo"/avançado prevalece).

        Retorna: "Aprovado" | "Cancelado" | "Reprovado" | "Pendente" | "Desconhecido"
        """
        url = f"{self._base_url()}/wit/workitems/{work_item_id}?$expand=all&api-version={API_VERSION}"
        response = self.session.get(url, headers=self.headers_json, timeout=60)
        data = self._handle_response(response, f"Buscar status do Work Item {work_item_id}")

        child_ids = []
        for rel in data.get("relations") or []:
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward":  # "Child"
                try:
                    child_ids.append(int(rel.get("url", "").rstrip("/").split("/")[-1]))
                except (ValueError, IndexError):
                    continue

        items_fields = [{
            "board_column": data.get("fields", {}).get("System.BoardColumn", ""),
            "description": data.get("fields", {}).get("System.Description", ""),
        }]

        if child_ids:
            ids_param = ",".join(str(c) for c in child_ids)
            fields2 = "System.Id,System.BoardColumn,System.Description"
            url2 = f"{self._base_url()}/wit/workitems?ids={ids_param}&fields={fields2}&api-version={API_VERSION}"
            response2 = self.session.get(url2, headers=self.headers_json, timeout=60)
            data2 = self._handle_response(response2, f"Buscar status dos filhos do Work Item {work_item_id}")
            for wi in data2.get("value", []):
                items_fields.append({
                    "board_column": wi.get("fields", {}).get("System.BoardColumn", ""),
                    "description": wi.get("fields", {}).get("System.Description", ""),
                })

        found = set()
        for item in items_fields:
            status = self._classify_board_column(item["board_column"])
            if status is None:
                status = self._classify_description_text(item["description"])
            if status:
                found.add(status)

        priority = ["Cancelado", "Reprovado", "Aprovado", "Pendente"]
        for status in priority:
            if status in found:
                return status
        return "Desconhecido"
