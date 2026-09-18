"""
"Reconhecer a API": sondagens sem credencial pra descobrir o que a IA não
tem como saber a partir de uma User Story — qual variante de rota existe de
verdade, como a API formata erros (chave i18n? `errors.<campo>`?), quais
rotas exigem token. O resultado vira texto pras "Observações" da geração.

Só monta as sondas e interpreta as respostas; quem executa é o mesmo
caminho da bateria (navegador ou servidor), pra passar pelo WAF.
"""
import json
import re
from urllib.parse import urlparse

_RE_METODO_ROTA = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}.:]+)", re.I)
_RE_ROTA_SOLTA = re.compile(r"(?<![\w/])(/api/[A-Za-z0-9_\-/{}.:]+)")
_RE_I18N = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

# Quando a especificação não cita nenhuma rota, sonda as convencionais.
_ROTAS_PADRAO = [("POST", "/api/v1/auth/login"), ("GET", "/api/v1/me"), ("POST", "/api/v1/auth/logout")]
_MAX_ROTAS = 8


def extrair_rotas(especificacao: str) -> list:
    """[(metodo, caminho)] citados no texto, na ordem, sem repetição."""
    vistos, rotas = set(), []
    for m in _RE_METODO_ROTA.finditer(especificacao or ""):
        par = (m.group(1).upper(), m.group(2).rstrip(".,;:)"))
        if par not in vistos:
            vistos.add(par)
            rotas.append(par)
    for m in _RE_ROTA_SOLTA.finditer(especificacao or ""):
        caminho = m.group(1).rstrip(".,;:)")
        if not any(c == caminho for _, c in rotas):
            metodo = "POST" if re.search(r"login|logout|auth|create|cadastr", caminho, re.I) else "GET"
            rotas.append((metodo, caminho))
    return rotas[:_MAX_ROTAS]


def _variantes(caminho: str) -> list:
    """Variantes plausíveis: como está, com /v1 inserido, com /api/v1 na frente."""
    v = [caminho]
    if caminho.startswith("/api/") and not re.match(r"^/api/v\d+/", caminho):
        v.append("/api/v1" + caminho[len("/api"):])
    if not caminho.startswith("/api"):
        v.append("/api/v1" + caminho)
        v.append("/api" + caminho)
    saida = []
    for c in v:
        if c not in saida:
            saida.append(c)
    return saida


def montar_sondas(especificacao: str, base_url: str) -> list:
    """
    Lista de sondas no formato de caso do runner (sem asserções, sem
    credencial): [{"id","nome","metodo","url","headers","body","extrair","rota_original","variante"}].
    """
    base = (base_url or "").rstrip("/")
    rotas = extrair_rotas(especificacao) or list(_ROTAS_PADRAO)
    sondas = []
    for metodo, caminho in rotas:
        for variante in _variantes(caminho):
            sondas.append({
                "id": f"sonda-{len(sondas) + 1}",
                "nome": f"{metodo} {variante}",
                "metodo": metodo,
                "url": base + variante,
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
                "body": "{}" if metodo in ("POST", "PUT", "PATCH") else "",
                "extrair": [],
                "rota_original": caminho,
                "variante": variante,
            })
    return sondas


def _json_ou_none(texto):
    try:
        return json.loads(texto)
    except Exception:
        return None


def analisar(sondas: list, respostas: list) -> dict:
    """
    Interpreta as respostas (mesmo formato que o executor devolve) e monta
    {"observacoes": str, "tabela": [{"sonda","status","conclusao"}], "rotas_reais": {rota_original: variante}}.
    """
    tabela, rotas_reais, fatos = [], {}, []
    formato_msg_i18n = None
    tem_errors_por_campo = False
    exige_bearer = []
    for s, r in zip(sondas, respostas or []):
        status = r.get("status")
        corpo = r.get("body") or ""
        ctype = ""
        for k, v in (r.get("response_headers") or r.get("headers") or {}).items():
            if k.lower() == "content-type":
                ctype = str(v).lower()
        js = _json_ou_none(corpo) if corpo else None
        if status is None:
            conclusao = f"sem resposta ({r.get('erro') or 'falha de rede'})"
        elif js is None or "json" not in ctype:
            conclusao = "não é rota da API (resposta não-JSON — estático/CDN)" if status in (403, 404, 405) else f"resposta não-JSON ({status})"
        elif status == 404:
            conclusao = "rota não existe (404 JSON)"
        else:
            conclusao = f"rota existe ({status})"
            if s["rota_original"] not in rotas_reais:
                rotas_reais[s["rota_original"]] = s["variante"]
            msg = js.get("message") if isinstance(js, dict) else None
            if isinstance(msg, str) and formato_msg_i18n is None:
                formato_msg_i18n = bool(_RE_I18N.match(msg.strip()))
            if isinstance(js, dict) and isinstance(js.get("errors"), dict):
                tem_errors_por_campo = True
            if status == 401:
                exige_bearer.append(s["variante"])
        tabela.append({"sonda": s["nome"], "status": status if status is not None else "—", "conclusao": conclusao})

    for original, real in rotas_reais.items():
        if real != original:
            fatos.append(f"A rota documentada {original} não existe; a rota real é {real} — use {{{{base_url}}}}{real}.")
        else:
            fatos.append(f"A rota {real} existe e responde JSON — use {{{{base_url}}}}{real}.")
    inexistentes = [s["rota_original"] for s in sondas if s["rota_original"] not in rotas_reais]
    for rota in sorted(set(inexistentes)):
        fatos.append(f"Nenhuma variante de {rota} respondeu como API — não gere casos para essa rota (ou confirme o caminho antes).")
    if formato_msg_i18n is True:
        fatos.append('As mensagens de erro vêm no campo "message" como CHAVE i18n (ex.: api.auth.invalid_credentials) — não compare textos de mensagem, apenas verifique que "message" existe (json_exists) ou compare com json_equals_var.')
    elif formato_msg_i18n is False:
        fatos.append('As mensagens de erro vêm no campo "message" como texto — prefira json_exists em "message" a comparar o texto exato.')
    if tem_errors_por_campo:
        fatos.append('Erros de validação (422) vêm em "errors.<campo>" como lista de strings — use json_exists em errors.<campo>; não existe campo "error" no singular.')
    if exige_bearer:
        fatos.append("Rotas protegidas (respondem 401 sem token): " + ", ".join(sorted(set(exige_bearer))) + ' — envie "Authorization: Bearer {{auth_token}}" extraído do login.')
    if not fatos:
        fatos.append("Nenhuma rota citada respondeu como API a partir desta Base URL — confira a Base URL e as rotas antes de gerar.")
    return {"observacoes": "\n".join(f"- {f}" for f in fatos), "tabela": tabela, "rotas_reais": rotas_reais}
