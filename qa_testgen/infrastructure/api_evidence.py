"""
Evidências dos Testes de API: mascaramento de segredos, relatório em
Markdown e pacote .zip com uma pasta por caso (request, response,
resultado) — o mesmo formato que se produz "na mão" ao testar no Postman,
só que gerado automaticamente.
"""
import io
import json
import re
import unicodedata
import zipfile
from datetime import datetime

from qa_testgen.config import TZ_BR
from qa_testgen.domain.models.api_test import ApiCaseResult


MASCARA = "***MASCARADO***"


class ApiEvidenceBuilder:

    # ---- Mascaramento ------------------------------------------------------
    _RE_JSON_SENSIVEL = re.compile(
        r'("(?:password|senha|token|access_token|refresh_token|secret|api_key|apikey|authorization)"\s*:\s*")([^"]*)(")',
        re.IGNORECASE,
    )
    _RE_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-|]+", re.IGNORECASE)
    _RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

    @classmethod
    def mascarar(cls, texto: str, segredos: list = None) -> str:
        """
        Esconde valores de campos sensíveis em JSON, tokens Bearer, JWTs e
        qualquer valor listado em `segredos` (ex.: senhas das variáveis
        marcadas como secretas). Nunca devolve o segredo em claro.
        """
        if not texto:
            return texto or ""
        saida = texto
        for segredo in sorted({s for s in (segredos or []) if s and len(s) >= 3}, key=len, reverse=True):
            saida = saida.replace(segredo, MASCARA)
        saida = cls._RE_JSON_SENSIVEL.sub(lambda m: f"{m.group(1)}{MASCARA}{m.group(3)}", saida)
        saida = cls._RE_BEARER.sub(lambda m: f"{m.group(1)}{MASCARA}", saida)
        saida = cls._RE_JWT.sub(MASCARA, saida)
        return saida

    @classmethod
    def mascarar_headers(cls, headers: dict, segredos: list = None) -> dict:
        saida = {}
        for k, v in (headers or {}).items():
            if k.lower() in ("authorization", "x-api-key", "api-key", "cookie", "set-cookie"):
                saida[k] = MASCARA if k.lower() != "authorization" else cls._RE_BEARER.sub(lambda m: f"{m.group(1)}{MASCARA}", str(v)) if "bearer" in str(v).lower() else MASCARA
            else:
                saida[k] = cls.mascarar(str(v), segredos)
        return saida

    # ---- Utilitários ---------------------------------------------------------
    @staticmethod
    def slug(texto: str, limite: int = 70) -> str:
        base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
        base = re.sub(r"\(([^)]*)\)", r"_\1", base)
        base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
        return base[:limite].strip("-") or "caso"

    @staticmethod
    def _json_bonito(texto: str) -> str:
        try:
            return json.dumps(json.loads(texto), ensure_ascii=False, indent=2)
        except Exception:
            return texto or ""

    @staticmethod
    def resumo(resultados: list) -> dict:
        total = len(resultados)
        aprovados = sum(1 for r in resultados if r.passou)
        pulados = sum(1 for r in resultados if r.pulado)
        erros = sum(1 for r in resultados if r.erro)
        reprovados = total - aprovados - pulados - erros
        assercoes = sum(len(r.assercoes) for r in resultados)
        assercoes_ok = sum(1 for r in resultados for a in r.assercoes if a.passou)
        tempos = [r.tempo_ms for r in resultados if not r.pulado and r.status_code is not None]
        return {
            "total": total, "aprovados": aprovados, "reprovados": reprovados,
            "erros": erros, "pulados": pulados,
            "assercoes": assercoes, "assercoes_ok": assercoes_ok,
            "tempo_medio_ms": int(sum(tempos) / len(tempos)) if tempos else 0,
            "tempo_min_ms": min(tempos) if tempos else 0,
            "tempo_max_ms": max(tempos) if tempos else 0,
            "status_geral": "Aprovado" if total and aprovados == total - pulados and not erros and reprovados == 0 else "Reprovado",
        }

    # ---- Textos por caso -----------------------------------------------------
    @classmethod
    def texto_request(cls, r: ApiCaseResult, segredos: list = None) -> str:
        linhas = [f"{r.metodo} {cls.mascarar(r.url_final, segredos)}"]
        for k, v in cls.mascarar_headers(r.request_headers, segredos).items():
            linhas.append(f"{k}: {v}")
        linhas.append("")
        linhas.append(cls.mascarar(cls._json_bonito(r.request_body), segredos))
        return "\n".join(linhas).rstrip() + "\n"

    @classmethod
    def mascarar_somente_segredos(cls, texto: str, segredos: list = None) -> str:
        """
        Só troca os VALORES secretos conhecidos — sem as regras genéricas
        (Bearer/JWT/campos sensíveis). Usada na definição da bateria que vai
        no .zip: lá `Authorization: Bearer {{auth_token}}` é um modelo, não
        um segredo, e mascará-lo inutilizava o arquivo pra repetir a bateria.
        """
        if not texto:
            return texto or ""
        saida = texto
        for segredo in sorted({s for s in (segredos or []) if s and len(s) >= 3}, key=len, reverse=True):
            saida = saida.replace(segredo, MASCARA)
        return saida

    @classmethod
    def texto_response(cls, r: ApiCaseResult, segredos: list = None) -> str:
        if r.bloqueado:
            return f"NÃO EXECUTADO — {r.motivo_pulo}\n"
        if r.pulado:
            return "Caso desabilitado — não executado.\n"
        if r.status_code is None:
            return f"SEM RESPOSTA ({r.tempo_ms} ms)\n\n{r.erro}\n"
        linhas = [f"HTTP {r.status_code} {r.status_text}  ({r.tempo_ms} ms)"]
        for k, v in cls.mascarar_headers(r.response_headers, segredos).items():
            linhas.append(f"{k}: {v}")
        linhas.append("")
        linhas.append(cls.mascarar(cls._json_bonito(r.response_body), segredos))
        return "\n".join(linhas).rstrip() + "\n"

    @classmethod
    def texto_resultado(cls, r: ApiCaseResult) -> str:
        linhas = [
            f"Caso: {r.nome}",
            f"Status HTTP: {r.status_code if r.status_code is not None else '—'} ({r.tempo_ms} ms)",
            f"Resultado: {r.resultado_label}",
        ]
        if r.erro:
            linhas.append(f"Erro: {r.erro}")
        if r.bloqueado:
            linhas.append(f"Motivo: {r.motivo_pulo}")
        linhas.append("")
        linhas.append("Asserções:")
        for a in r.assercoes:
            linhas.append(f"{'PASSOU' if a.passou else 'FALHOU'}  {a.descricao}")
            if not a.passou and a.detalhe:
                linhas.append(f"         -> {a.detalhe}")
        return "\n".join(linhas) + "\n"

    # ---- Relatório Markdown ------------------------------------------------
    @classmethod
    def gerar_markdown(cls, projeto: str, ambiente: str, base_url: str, resultados: list,
                       contexto: str = "", documentos: list = None, observacoes: str = "",
                       autor: str = "", segredos: list = None, imagens_por_caso: dict = None,
                       analise_md: list = None) -> str:
        """analise_md: linhas da seção "Análise automática" (api_triage.markdown_da_analise), opcional."""
        res = cls.resumo(resultados)
        agora = datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M")
        md = [f"# Relatório de Testes de API — {projeto}", ""]
        md.append(f"**Ambiente:** {ambiente or '—'}  ")
        md.append(f"**Base URL:** `{cls.mascarar(base_url or '—', segredos)}`  ")
        md.append(f"**Data:** {agora}  ")
        if autor:
            md.append(f"**Executor:** {autor}  ")
        md.append(f"**Status geral:** {'✅' if res['status_geral'] == 'Aprovado' else '❌'} **{res['status_geral']}**")
        md += ["", "---", "", "## 1. Resumo", "", "| Métrica | Valor |", "|---|---|"]
        md.append(f"| Casos executados | {res['total'] - res['pulados']} de {res['total']}" + (f" ({res['pulados']} não executado(s) — desabilitado(s) ou bloqueado(s))" if res['pulados'] else "") + " |")
        md.append(f"| Aprovados / Reprovados / Erros | {res['aprovados']} / {res['reprovados']} / {res['erros']} |")
        md.append(f"| Asserções | {res['assercoes']} executadas · {res['assercoes_ok']} passaram · {res['assercoes'] - res['assercoes_ok']} falharam |")
        md.append(f"| Tempo de resposta | médio {res['tempo_medio_ms']} ms · mín. {res['tempo_min_ms']} ms · máx. {res['tempo_max_ms']} ms |")

        if contexto or documentos:
            md += ["", "## 2. Contexto", ""]
            if contexto:
                md.append(contexto.strip())
                md.append("")
            if documentos:
                md.append("**Documentos de apoio:** " + ", ".join(f"`{d}`" for d in documentos))

        md += ["", f"## {3 if (contexto or documentos) else 2}. Resultados por caso", "",
               "| # | Caso | Método | Status | Tempo | Asserções | Resultado |", "|---|---|---|---|---|---|---|"]
        for idx, r in enumerate(resultados, start=1):
            ok = sum(1 for a in r.assercoes if a.passou)
            icone = "✅" if r.passou else ("⏸️" if r.pulado else "❌")
            md.append(f"| {idx} | {r.nome} | `{r.metodo}` | {r.status_code if r.status_code is not None else '—'} | {r.tempo_ms} ms | {ok}/{len(r.assercoes)} | {icone} {r.resultado_label} |")

        sec = 4 if (contexto or documentos) else 3
        if analise_md:
            md += ["", f"## {sec}. " + analise_md[0].lstrip("# ").split(". ", 1)[-1]] + [cls.mascarar(l, segredos) for l in analise_md[1:]]
            sec += 1
        md += ["", f"## {sec}. Detalhes e evidências", ""]
        for idx, r in enumerate(resultados, start=1):
            md.append(f"### {idx}. {r.nome} — {r.resultado_label}")
            md.append("")
            md.append(f"`{r.metodo} {cls.mascarar(r.url_final, segredos)}`")
            md.append("")
            md.append("**Asserções**")
            md.append("")
            for a in r.assercoes:
                md.append(f"- {'✅' if a.passou else '❌'} {a.descricao}" + (f" — _{a.detalhe}_" if (not a.passou and a.detalhe) else ""))
            if r.erro:
                md.append(f"- ⚠️ Erro: {r.erro}")
            if r.bloqueado:
                md.append(f"- ⛔ {r.motivo_pulo}")
            md.append("")
            if not r.pulado:
                md += ["<details><summary>Request</summary>", "", "```http", cls.texto_request(r, segredos).rstrip(), "```", "", "</details>", ""]
                md += ["<details><summary>Response</summary>", "", "```http", cls.texto_response(r, segredos).rstrip(), "```", "", "</details>", ""]
            imgs = (imagens_por_caso or {}).get(r.case_id) or []
            if imgs:
                md.append("**Imagens anexadas:** " + ", ".join(f"`{nome}`" for nome, _b in imgs))
                md.append("")

        if observacoes:
            md += [f"## {sec + 1}. Observações e próximos passos", "", observacoes.strip(), ""]
        md += ["---", "", "_Gerado automaticamente pelo QA TestGen — módulo Testes de API._", ""]
        return "\n".join(md)

    # ---- Pacote ZIP ----------------------------------------------------------
    @classmethod
    def gerar_zip(cls, projeto: str, resultados: list, relatorio_md: str, relatorio_pdf: bytes = None,
                  segredos: list = None, imagens_por_caso: dict = None, definicao_json: str = None) -> bytes:
        """
        Estrutura:
          <slug-projeto>_<data>/
            RELATORIO.md, RELATORIO.pdf, indice.json, definicao-testes.json
            NN_<slug-caso>/1_request.txt, 2_response.txt, 3_resultado.txt, imagens...
        """
        carimbo = datetime.now(TZ_BR).strftime("%Y-%m-%d_%H%M")
        raiz = f"{cls.slug(projeto, 40)}_{carimbo}"
        buffer = io.BytesIO()
        indice = []
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{raiz}/RELATORIO.md", relatorio_md.encode("utf-8"))
            if relatorio_pdf:
                z.writestr(f"{raiz}/RELATORIO.pdf", relatorio_pdf)
            if definicao_json:
                z.writestr(f"{raiz}/definicao-testes.json", cls.mascarar_somente_segredos(definicao_json, segredos).encode("utf-8"))
            for idx, r in enumerate(resultados, start=1):
                pasta = f"{raiz}/{idx:02d}_{cls.slug(r.nome)}"
                z.writestr(f"{pasta}/1_request.txt", cls.texto_request(r, segredos).encode("utf-8"))
                z.writestr(f"{pasta}/2_response.txt", cls.texto_response(r, segredos).encode("utf-8"))
                z.writestr(f"{pasta}/3_resultado.txt", cls.texto_resultado(r).encode("utf-8"))
                for nome, conteudo in (imagens_por_caso or {}).get(r.case_id) or []:
                    z.writestr(f"{pasta}/{nome}", conteudo)
                indice.append({
                    "n": idx, "caso": r.nome, "metodo": r.metodo, "status": r.status_code,
                    "tempo_ms": r.tempo_ms, "resultado": r.resultado_label,
                    "assercoes": len(r.assercoes), "assercoes_ok": sum(1 for a in r.assercoes if a.passou),
                    "pasta": pasta.split("/", 1)[1],
                })
            z.writestr(f"{raiz}/indice.json", json.dumps({"projeto": projeto, "gerado_em": carimbo, "resumo": cls.resumo(resultados), "casos": indice}, ensure_ascii=False, indent=2).encode("utf-8"))
        return buffer.getvalue()
