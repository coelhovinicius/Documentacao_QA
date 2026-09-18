"""
Converte uma bateria de Testes de API (casos + resultados da execução) no
formato do assistente de QA — Matriz de Cobertura, Casos de Teste e Planos
— pra que ela siga pelo Passo 7 como qualquer outra documentação (vincular
a Work Items, suítes estáticas, reconciliar).
"""
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from qa_testgen.config import TZ_BR

_MASCARA = "***"


def _sigla(ambiente: str) -> str:
    return {"Homologação": "HML", "Produção": "PROD"}.get(ambiente or "", "")


def _endpoint(caso: dict, base_url: str, valores: dict = None) -> str:
    """'POST /api/v1/auth/login' a partir da url do caso (com {{base_url}} ou absoluta)."""
    url = (caso.get("url") or "").replace("{{base_url}}", "").replace("{{ base_url }}", "")
    for nome, valor in (valores or {}).items():
        if valor:
            url = url.replace("{{" + nome + "}}", str(valor))
    if url.startswith(("http://", "https://")):
        url = urlparse(url).path or "/"
    if base_url and url.startswith(base_url):
        url = url[len(base_url):] or "/"
    return f"{(caso.get('metodo') or 'GET').upper()} {url or '/'}"


def _status_esperado(caso: dict):
    for a in caso.get("assercoes") or []:
        if a.get("tipo") == "status":
            try:
                return int(str(a.get("valor")).strip())
            except ValueError:
                return None
    return None


def _descrever_assercao(a: dict) -> str:
    tipo, alvo, valor = a.get("tipo"), a.get("alvo") or "", a.get("valor") or ""
    return {
        "status": f"HTTP {valor}",
        "json_exists": f"campo '{alvo}' presente",
        "json_absent": f"campo '{alvo}' ausente",
        "json_equals": f"'{alvo}' = {valor}",
        "json_not_empty": f"'{alvo}' não vazio",
        "json_type": f"'{alvo}' do tipo {valor}",
        "json_contains": f"'{alvo}' contém '{valor}'",
        "json_equals_var": f"'{alvo}' igual ao valor guardado em {valor}",
        "header_contains": f"header '{alvo}' contém '{valor}'",
        "body_contains": f"corpo contém '{valor}'",
        "body_not_contains": f"corpo não contém '{valor}'",
        "response_time_max": f"tempo de resposta ≤ {valor} ms",
    }.get(tipo, a.get("descricao") or tipo)


def _mascarar_body(body: str, secretas: set) -> str:
    texto = body or ""
    for nome in secretas:
        texto = texto.replace("{{" + nome + "}}", _MASCARA)
    return texto


def converter_bateria(projeto: str, ambiente: str, base_url: str, casos: list, resultados: list = None,
                      variaveis: list = None, work_items: list = None, incluir_resultado: bool = True,
                      mc_inicio: int = 1) -> dict:
    """
    Devolve {"matriz": [...], "test_cases": [...], "test_plans": [...]} no
    formato exato que o assistente guarda em sessão.

    casos: dicts do módulo (ApiTestCase.to_dict()); só os habilitados entram.
    resultados: ApiCaseResult (opcional) — vira texto no caso se incluir_resultado.
    work_items: [{"id","title"}] escolhidos na geração — o primeiro vira o
                pré-vínculo (work_item_relacionado) de todos os casos.
    mc_inicio: número da primeira linha da Matriz (pra acrescentar a uma
               sessão que já tem linhas).
    """
    sigla = _sigla(ambiente)
    secretas = {v["nome"] for v in (variaveis or []) if v.get("secreto")}
    valores_publicos = {v["nome"]: v.get("valor") for v in (variaveis or []) if v.get("nome") and not v.get("secreto")}
    nomes_vars = sorted({v["nome"] for v in (variaveis or []) if v.get("nome")})
    res_por_id = {r.case_id: r for r in (resultados or [])}
    wi_id = str(work_items[0]["id"]) if work_items else ""
    agora = datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")

    matriz, test_cases, suites = [], [], {}
    n = mc_inicio
    for caso in casos:
        if not caso.get("habilitado", True):
            continue
        endpoint = _endpoint(caso, base_url, valores_publicos)
        status = _status_esperado(caso)
        mc_id = f"MC-{n:03d}" + (f" {sigla}" if sigla else "")
        n += 1
        negativo = status is not None and status >= 400
        prioridade = "Alta" if (status in (200, 201, 401, 403) or status is None) else "Média"
        matriz.append({
            "id": mc_id, "funcionalidade": endpoint, "requisito": caso.get("descricao") or f"Contrato de {endpoint}",
            "cenario": caso.get("nome", ""), "categoria": "Negativo" if negativo else "Positivo",
            "prioridade": prioridade, "criticidade": prioridade, "observacoes": "Origem: Testes de API",
        })

        headers = caso.get("headers") or {}
        headers_txt = "; ".join(f"{k}: {('Bearer ' + _MASCARA) if k.lower() == 'authorization' else v}" for k, v in headers.items())
        body_txt = _mascarar_body(caso.get("body") or "", secretas)
        acao = f"Enviar {endpoint}"
        if headers_txt:
            acao += f" com headers [{headers_txt}]"
        if body_txt.strip():
            acao += f" e body {body_txt.strip()}"
        esperado = "; ".join(_descrever_assercao(a) for a in (caso.get("assercoes") or [])) or "Resposta conforme contrato"
        passos = [{"numero": 1, "acao": acao, "resultado_esperado": esperado}]
        for ex in caso.get("extrair") or []:
            passos.append({"numero": len(passos) + 1, "acao": f"Guardar '{ex.get('caminho')}' da resposta como {{{{{ex.get('nome')}}}}}",
                           "resultado_esperado": "Valor disponível para os próximos casos"})

        pre = [f"Base URL: {base_url}" if base_url else "Base URL definida no ambiente", f"Ambiente: {ambiente or '—'}"]
        usadas = sorted(set(re.findall(r"\{\{\s*([\w.-]+)\s*\}\}", " ".join([caso.get("url") or "", caso.get("body") or ""] + list(headers.values())))) - {"base_url"})
        if usadas:
            pre.append("Variáveis: " + ", ".join(f"{u} (secreta)" if u in secretas else u for u in usadas))
        r = res_por_id.get(caso.get("id"))
        if incluir_resultado and r is not None and not r.pulado:
            ok = sum(1 for a in r.assercoes if a.passou)
            pre.append(f"Última execução ({sigla or ambiente or '—'}, {agora}): {r.resultado_label} — {ok}/{len(r.assercoes)} asserções"
                       + (f", HTTP {r.status_code}" if r.status_code is not None else ""))

        tc = {
            "titulo": caso.get("nome", ""), "pre_condicoes": "\n".join(pre), "passos": passos,
            "requisitos_relacionados": [mc_id], "origem": "testes_api", "api_case_id": caso.get("id"),
        }
        if wi_id:
            tc["work_item_relacionado"] = wi_id
        test_cases.append(tc)
        suites.setdefault(endpoint, []).append(tc["titulo"])

    plano = {
        "nome": f"Testes de API — {projeto or 'bateria'}",
        "descricao": f"Bateria de testes de API gerada no QA TestGen ({ambiente or '—'}). Uma suíte por endpoint.",
        "suites": [{"nome": ep, "descricao": f"Casos do endpoint {ep}", "casos": titulos} for ep, titulos in suites.items()],
    }
    return {"matriz": matriz, "test_cases": test_cases, "test_plans": [plano] if test_cases else []}


def resultados_para_test_run(casos: list, resultados: list) -> dict:
    """{titulo_do_caso: {"outcome": "Passed"|"Failed"|"NotApplicable", "comentario": str}} pro Test Run."""
    res_por_id = {r.case_id: r for r in (resultados or [])}
    saida = {}
    for caso in casos:
        r = res_por_id.get(caso.get("id"))
        if r is None or r.pulado:
            continue
        falhas = [f"{a.descricao}: {a.detalhe}" for a in r.assercoes if not a.passou]
        saida[caso.get("nome", "")] = {
            "outcome": "Passed" if r.passou else "Failed",
            "comentario": (f"HTTP {r.status_code} em {r.tempo_ms} ms. " if r.status_code is not None else "") +
                          (("Falhas: " + " | ".join(falhas)) if falhas else "Todas as asserções passaram.") +
                          (f" Erro: {r.erro}" if r.erro else ""),
        }
    return saida
