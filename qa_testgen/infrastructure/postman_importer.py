"""
Importa collections e environments do Postman (schema v2.1) pro formato
do módulo Testes de API.

Os scripts de teste do Postman são JavaScript e o app roda em Python puro
(sem Node no Streamlit Cloud). Então o importador NÃO executa scripts:
ele reconhece, por padrão de texto, as asserções mais comuns
(`pm.response.to.have.status(200)`, `pm.expect(body.x).to.have.property('y')`
etc.) e converte pro formato declarativo do runner. O que não conseguir
converter vira um aviso no caso — a pessoa complementa na tela.
"""
import json
import re
import uuid

from qa_testgen.domain.models.api_test import (
    ApiAssertion, ApiTestCase, ApiVariableExtraction,
)


class PostmanImportError(Exception):
    pass


class PostmanImporter:

    # ---- Environment ------------------------------------------------------
    @staticmethod
    def parse_environment(raw: bytes) -> list:
        """
        Retorna [{"nome", "valor", "secreto"}] a partir de um
        *.postman_environment.json. Valores de variáveis do tipo "secret"
        vêm marcados como secretas (nunca vão pra evidência).
        """
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception as error:
            raise PostmanImportError(f"Environment inválido (não é JSON): {error}")
        values = data.get("values")
        if not isinstance(values, list):
            raise PostmanImportError("Environment sem a lista 'values' — é mesmo um export de environment do Postman?")
        variaveis = []
        for item in values:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            if item.get("enabled") is False:
                continue
            variaveis.append({
                "nome": str(item["key"]),
                "valor": str(item.get("value", "") or ""),
                "secreto": str(item.get("type", "")).lower() == "secret" or "password" in str(item["key"]).lower(),
            })
        return variaveis

    # ---- Collection -------------------------------------------------------
    @classmethod
    def parse_collection(cls, raw: bytes) -> dict:
        """
        Retorna {"nome", "descricao", "variaveis": [...], "casos": [ApiTestCase...]}.
        Pastas aninhadas são achatadas mantendo a ordem (nome da pasta vira
        prefixo do caso, pra não perder o contexto).
        """
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception as error:
            raise PostmanImportError(f"Collection inválida (não é JSON): {error}")
        info = data.get("info") or {}
        if "item" not in data:
            raise PostmanImportError("Collection sem 'item' — é mesmo um export v2.1 do Postman?")

        variaveis = []
        for var in data.get("variable") or []:
            if isinstance(var, dict) and var.get("key"):
                variaveis.append({
                    "nome": str(var["key"]),
                    "valor": str(var.get("value", "") or ""),
                    "secreto": "password" in str(var["key"]).lower() or "token" in str(var["key"]).lower(),
                })

        casos = []
        cls._walk_items(data["item"], "", casos)
        return {
            "nome": str(info.get("name", "") or "Collection importada"),
            "descricao": str(info.get("description", "") or ""),
            "variaveis": variaveis,
            "casos": casos,
        }

    @classmethod
    def _walk_items(cls, items: list, prefixo: str, saida: list) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if "item" in item and "request" not in item:
                novo_prefixo = f"{prefixo}{item.get('name', '')} / " if item.get("name") else prefixo
                cls._walk_items(item["item"], novo_prefixo, saida)
                continue
            if "request" in item:
                saida.append(cls._item_to_case(item, prefixo))

    @classmethod
    def _item_to_case(cls, item: dict, prefixo: str) -> ApiTestCase:
        req = item.get("request") or {}
        if isinstance(req, str):
            req = {"method": "GET", "url": req}

        metodo = str(req.get("method", "GET")).upper()
        url = cls._url_to_string(req.get("url"))

        headers = {}
        for h in req.get("header") or []:
            if isinstance(h, dict) and h.get("key") and not h.get("disabled"):
                headers[str(h["key"])] = str(h.get("value", "") or "")

        # Auth bearer definido no request vira header Authorization.
        auth = req.get("auth") or {}
        if isinstance(auth, dict) and auth.get("type") == "bearer" and "Authorization" not in headers:
            for entry in auth.get("bearer") or []:
                if isinstance(entry, dict) and entry.get("key") == "token":
                    headers["Authorization"] = f"Bearer {entry.get('value', '')}"

        body = cls._body_to_string(req.get("body"), headers)

        assercoes, extrair, avisos = [], [], []
        for event in item.get("event") or []:
            if not isinstance(event, dict) or event.get("listen") != "test":
                continue
            exec_lines = (event.get("script") or {}).get("exec") or []
            script = "\n".join(exec_lines) if isinstance(exec_lines, list) else str(exec_lines)
            a, e, w = cls.convert_test_script(script)
            assercoes.extend(a)
            extrair.extend(e)
            avisos.extend(w)

        if not assercoes:
            avisos.append("Nenhuma asserção reconhecida — adicione pelo menos o status esperado.")

        return ApiTestCase(
            id=str(uuid.uuid4()),
            nome=f"{prefixo}{item.get('name', 'Sem nome')}",
            metodo=metodo,
            url=url,
            headers=headers,
            body=body,
            assercoes=assercoes,
            extrair=extrair,
            descricao=str((item.get("request") or {}).get("description", "") or "") if isinstance(item.get("request"), dict) else "",
            avisos=avisos,
        )

    @staticmethod
    def _url_to_string(url) -> str:
        if url is None:
            return ""
        if isinstance(url, str):
            return url
        if isinstance(url, dict):
            if url.get("raw"):
                return str(url["raw"])
            host = url.get("host") or []
            path = url.get("path") or []
            host_s = ".".join(host) if isinstance(host, list) else str(host)
            path_s = "/".join(path) if isinstance(path, list) else str(path)
            proto = url.get("protocol")
            base = f"{proto}://{host_s}" if proto else host_s
            return f"{base}/{path_s}" if path_s else base
        return str(url)

    @staticmethod
    def _body_to_string(body, headers: dict) -> str:
        if not isinstance(body, dict):
            return ""
        mode = body.get("mode")
        if mode == "raw":
            return str(body.get("raw", "") or "")
        if mode == "urlencoded":
            pares = [
                f"{p.get('key')}={p.get('value', '')}"
                for p in body.get("urlencoded") or []
                if isinstance(p, dict) and p.get("key") and not p.get("disabled")
            ]
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            return "&".join(pares)
        if mode == "formdata":
            # form-data com arquivos não é suportado; manda como urlencoded
            # os campos de texto e avisa via header comentário.
            pares = [
                f"{p.get('key')}={p.get('value', '')}"
                for p in body.get("formdata") or []
                if isinstance(p, dict) and p.get("key") and p.get("type", "text") == "text" and not p.get("disabled")
            ]
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            return "&".join(pares)
        if mode == "graphql":
            gql = body.get("graphql") or {}
            headers.setdefault("Content-Type", "application/json")
            return json.dumps({"query": gql.get("query", ""), "variables": gql.get("variables") or {}})
        return ""

    # ---- Conversão de scripts JS ----------------------------------------
    _RE_STATUS = re.compile(r"to\.have\.status\(\s*(\d{3})\s*\)")
    _RE_STATUS_CODE_EQL = re.compile(r"pm\.response\.code\)?\s*\.to\.(?:eql|equal)\(\s*(\d{3})\s*\)")
    _RE_HAS_PROP = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*(?:,\s*['\"][^'\"]*['\"])?\s*\)\s*\.to\.have\.property\(\s*['\"]([^'\"]+)['\"]\s*\)")
    _RE_NOT_HAS_PROP = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*(?:,\s*['\"][^'\"]*['\"])?\s*\)\s*\.to\.not\.have\.property\(\s*['\"]([^'\"]+)['\"]\s*\)")
    _RE_EQL = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*(?:,\s*['\"][^'\"]*['\"])?\s*\)\s*\.to\.(?:eql|equal)\(\s*(['\"]?)([^'\")]*)\2\s*\)")
    _RE_TYPE = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*(?:,\s*['\"][^'\"]*['\"])?\s*\)\s*\.to\.be\.(?:a|an)\(\s*['\"](\w+)['\"]\s*\)")
    _RE_NOT_EMPTY = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*(?:,\s*['\"][^'\"]*['\"])?\s*\)[^;\n]*\.not\.empty")
    _RE_SET_VAR = re.compile(r"pm\.(?:collectionVariables|environment|variables|globals)\.set\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z_][\w.\[\]']*)\s*\)")
    _RE_GET_VAR_EQL = re.compile(r"pm\.expect\(\s*([A-Za-z_][\w.\[\]']*)\s*\)\s*\.to\.(?:eql|equal)\(\s*(\w+)\s*\)")
    _RE_PREV_GET = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*pm\.(?:collectionVariables|environment|variables|globals)\.get\(\s*['\"]([^'\"]+)['\"]\s*\)")
    _RE_TEST_NAME = re.compile(r"pm\.test\(\s*['\"]([^'\"]+)['\"]")

    _JS_ROOTS = ("body", "json", "jsonData", "res", "response", "data", "d", "user")

    # `const d = pm.response.json().data || {}` / `const user = body.data.user`
    _RE_ALIAS = re.compile(
        r"(?:const|let|var)\s+(\w+)\s*=\s*\(?\s*(?:pm\.response\.json\(\)|res\.json\(\)|body|jsonData|json)((?:\.\w+)*)"
    )

    @classmethod
    def _raiz_conhecida(cls, expr: str, aliases: dict = None) -> bool:
        """
        `body.data.token` -> True; `r` (variável de um forEach) -> False.
        Sem raiz conhecida não existe caminho absoluto na resposta, então a
        asserção não é conversível (vira aviso, não regra errada).
        """
        raiz = re.split(r"[.\[]", expr.strip(), maxsplit=1)[0]
        return raiz in cls._JS_ROOTS or raiz in (aliases or {})

    @classmethod
    def _js_path_to_json_path(cls, expr: str, aliases: dict = None) -> str:
        """
        `body.data.user.email` -> `data.user.email`; `jsonData.errors['email']`
        -> `errors.email`; com alias `d` = `data`, `d.roles` -> `data.roles`.
        Se a expressão não parte de uma variável que represente o JSON da
        resposta, devolve como está.
        """
        expr = expr.strip()
        expr = re.sub(r"\[\s*['\"]([^'\"]+)['\"]\s*\]", r".\1", expr)
        partes = expr.split(".")
        aliases = aliases or {}
        if partes and partes[0] in aliases:
            prefixo = aliases[partes[0]]
            partes = ([prefixo] if prefixo else []) + partes[1:]
        elif partes and partes[0] in cls._JS_ROOTS:
            partes = partes[1:]
        return ".".join(p for p in partes if p)

    @classmethod
    def convert_test_script(cls, script: str):
        """
        Retorna (assercoes, extracoes, avisos). Heurístico por desenho:
        cobre os padrões de `pm.test` mais comuns e avisa sobre o resto.
        """
        assercoes, extracoes, avisos = [], [], []
        if not script or not script.strip():
            return assercoes, extracoes, avisos

        reconhecido = 0
        # alias -> caminho (ex.: {"d": "data", "user": "data.user"})
        aliases = {m.group(1): m.group(2).lstrip(".") for m in cls._RE_ALIAS.finditer(script)}

        for m in cls._RE_STATUS.finditer(script):
            assercoes.append(ApiAssertion("status", "", m.group(1), f"Status {m.group(1)}"))
            reconhecido += 1
        for m in cls._RE_STATUS_CODE_EQL.finditer(script):
            assercoes.append(ApiAssertion("status", "", m.group(1), f"Status {m.group(1)}"))
            reconhecido += 1

        for m in cls._RE_NOT_HAS_PROP.finditer(script):
            if not cls._raiz_conhecida(m.group(1), aliases):
                avisos.append(f"Asserção sobre '{m.group(1)}' ignorada (variável de laço JS, sem caminho na resposta).")
                continue
            base = cls._js_path_to_json_path(m.group(1), aliases)
            caminho = f"{base}.{m.group(2)}" if base else m.group(2)
            assercoes.append(ApiAssertion("json_absent", caminho, "", f"Campo '{caminho}' não existe"))
            reconhecido += 1
        for m in cls._RE_HAS_PROP.finditer(script):
            if not cls._raiz_conhecida(m.group(1), aliases):
                avisos.append(f"Asserção sobre '{m.group(1)}' ignorada (variável de laço JS, sem caminho na resposta).")
                continue
            base = cls._js_path_to_json_path(m.group(1), aliases)
            caminho = f"{base}.{m.group(2)}" if base else m.group(2)
            assercoes.append(ApiAssertion("json_exists", caminho, "", f"Campo '{caminho}' existe"))
            reconhecido += 1

        for m in cls._RE_TYPE.finditer(script):
            if not cls._raiz_conhecida(m.group(1), aliases):
                continue
            caminho = cls._js_path_to_json_path(m.group(1), aliases)
            if not caminho:
                continue
            assercoes.append(ApiAssertion("json_type", caminho, m.group(2), f"'{caminho}' é {m.group(2)}"))
            reconhecido += 1

        for m in cls._RE_NOT_EMPTY.finditer(script):
            caminho = cls._js_path_to_json_path(m.group(1), aliases)
            if caminho:
                assercoes.append(ApiAssertion("json_not_empty", caminho, "", f"'{caminho}' não vazio"))
                reconhecido += 1

        # Variáveis lidas antes de comparar (padrão: const prev = pm.x.get('msg'); expect(body.message).to.eql(prev))
        var_aliases = {m.group(1): m.group(2) for m in cls._RE_PREV_GET.finditer(script)}
        for m in cls._RE_GET_VAR_EQL.finditer(script):
            alias = m.group(2)
            if alias in var_aliases:
                caminho = cls._js_path_to_json_path(m.group(1), aliases)
                assercoes.append(ApiAssertion("json_equals_var", caminho, var_aliases[alias], f"'{caminho}' igual à variável {var_aliases[alias]}"))
                reconhecido += 1

        for m in cls._RE_EQL.finditer(script):
            caminho = cls._js_path_to_json_path(m.group(1), aliases)
            valor = m.group(3).strip()
            if not caminho or valor in var_aliases or re.fullmatch(r"[A-Za-z_]\w*", valor) and not m.group(2):
                continue  # comparação com variável JS — tratada acima ou não conversível
            assercoes.append(ApiAssertion("json_equals", caminho, valor, f"'{caminho}' = {valor}"))
            reconhecido += 1

        for m in cls._RE_SET_VAR.finditer(script):
            caminho = cls._js_path_to_json_path(m.group(2), aliases)
            if caminho:
                extracoes.append(ApiVariableExtraction(m.group(1), caminho))
                reconhecido += 1

        nomes_teste = cls._RE_TEST_NAME.findall(script)
        if nomes_teste and reconhecido < len(nomes_teste):
            avisos.append(
                f"Script com {len(nomes_teste)} pm.test e {reconhecido} regra(s) convertida(s) — "
                "revise as asserções; parte do JavaScript não tem equivalente automático."
            )
        return assercoes, extracoes, avisos
