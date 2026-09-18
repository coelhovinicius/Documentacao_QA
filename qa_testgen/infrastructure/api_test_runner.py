"""
Executor dos Testes de API — Python puro, com `requests`.

Roda os casos em ordem, resolvendo `{{variavel}}` em URL/headers/body,
avalia as asserções declarativas (ver ASSERTION_TYPES em
domain/models/api_test.py) e guarda variáveis extraídas da resposta pra
os casos seguintes (ex.: token do login usado na rota protegida).
"""
import json
import re
import ssl
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

from qa_testgen.domain.models.api_test import (
    ApiAssertion, ApiAssertionResult, ApiCaseResult, ApiTestCase,
)


class _HeadersCI(dict):
    """dict com .get() insensível a maiúsculas — o suficiente pra header_contains."""
    def get(self, chave, default=None):
        for k, v in self.items():
            if str(k).lower() == str(chave).lower():
                return v
        return default


class _SystemTrustAdapter(HTTPAdapter):
    """
    Usa o repositório de certificados do sistema operacional em vez do
    bundle do certifi. O requests recarrega o cacert.pem do certifi a cada
    pool novo — quando o venv fica num disco lento (ex.: pasta sincronizada
    em nuvem) isso custa dezenas de segundos por execução e estoura o
    timeout do primeiro caso. O contexto do sistema é criado uma vez só.
    """
    _CTX = None

    @classmethod
    def _contexto(cls):
        if cls._CTX is None:
            cls._CTX = ssl.create_default_context()
        return cls._CTX

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            pool_kwargs.pop("ca_certs", None)
            pool_kwargs.pop("ca_cert_dir", None)
            pool_kwargs["ssl_context"] = self._contexto()
        return host_params, pool_kwargs

    def cert_verify(self, conn, url, verify, cert):
        # O cert_verify original grava conn.ca_certs = certifi.where() (e faz
        # os.path.exists nele) mesmo quando o pool já tem ssl_context — é
        # isso que dispara a carga lenta. Com verify=True, deixa o contexto
        # do sistema cuidar da validação; nos demais casos, comportamento
        # padrão.
        if verify is True:
            conn.cert_reqs = "CERT_REQUIRED"
            conn.ca_certs = None
            conn.ca_cert_dir = None
            conn.ssl_context = self._contexto()
            if cert:
                super().cert_verify(conn, url, False, cert)
            return
        super().cert_verify(conn, url, verify, cert)


class ApiTestRunner:
    _RE_VAR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")

    def __init__(self, variaveis: dict, timeout: int = 30, verify_ssl: bool = True):
        # cópia — o runner altera conforme extrai variáveis
        self.variaveis = {str(k): ("" if v is None else str(v)) for k, v in (variaveis or {}).items()}
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        # Uma única Session por execução: o requests carrega o bundle de
        # certificados (certifi) uma vez por Session — com o venv num disco
        # lento isso custa segundos, então não vale pagar por caso.
        self.session = requests.Session()
        self.session.mount("https://", _SystemTrustAdapter())
        self.session.headers["User-Agent"] = "QA-TestGen-ApiTests/1.0"

    # ---- Variáveis ---------------------------------------------------------
    def substituir(self, texto: str) -> str:
        if not texto:
            return texto or ""

        def _sub(m):
            return self.variaveis.get(m.group(1), m.group(0))

        return self._RE_VAR.sub(_sub, texto)

    def variaveis_nao_resolvidas(self, texto: str) -> list:
        return [m for m in self._RE_VAR.findall(texto or "") if m not in self.variaveis]

    # ---- Caminho JSON simples ---------------------------------------------
    _AUSENTE = object()

    @classmethod
    def obter_caminho(cls, dado, caminho: str):
        """
        `data.user.email`, `errors.email[0]`, `items[2].id`. Retorna
        cls._AUSENTE quando qualquer trecho não existe.
        """
        atual = dado
        if not caminho:
            return atual
        tokens = re.findall(r"[^.\[\]]+|\[\d+\]", caminho)
        for tok in tokens:
            if tok.startswith("["):
                idx = int(tok[1:-1])
                if isinstance(atual, list) and 0 <= idx < len(atual):
                    atual = atual[idx]
                else:
                    return cls._AUSENTE
            else:
                if isinstance(atual, dict) and tok in atual:
                    atual = atual[tok]
                else:
                    return cls._AUSENTE
        return atual

    # ---- Execução ----------------------------------------------------------
    def executar(self, casos: list, on_progress: Optional[Callable[[int, int, ApiCaseResult], None]] = None) -> list:
        resultados = []
        total = len(casos)
        for idx, caso in enumerate(casos, start=1):
            if isinstance(caso, dict):
                caso = ApiTestCase.from_dict(caso)
            if not caso.habilitado:
                res = ApiCaseResult(
                    case_id=caso.id, nome=caso.nome, metodo=caso.metodo, url_final=caso.url,
                    request_headers={}, request_body="", status_code=None, status_text="",
                    response_headers={}, response_body="", tempo_ms=0, pulado=True,
                )
            else:
                res = self.executar_caso(caso)
            resultados.append(res)
            if on_progress:
                on_progress(idx, total, res)
        return resultados

    def executar_caso(self, caso: ApiTestCase) -> ApiCaseResult:
        url = self.substituir(caso.url)
        headers = {k: self.substituir(v) for k, v in (caso.headers or {}).items()}
        body = self.substituir(caso.body or "")

        faltando = self.variaveis_nao_resolvidas(caso.url) + [
            v for h in caso.headers.values() for v in self.variaveis_nao_resolvidas(h)
        ] + self.variaveis_nao_resolvidas(caso.body or "")
        aviso_vars = f"Variáveis não definidas: {', '.join(sorted(set(faltando)))}. " if faltando else ""

        resultado = ApiCaseResult(
            case_id=caso.id, nome=caso.nome, metodo=caso.metodo, url_final=url,
            request_headers=headers, request_body=body, status_code=None, status_text="",
            response_headers={}, response_body="", tempo_ms=0,
        )

        inicio = time.perf_counter()
        try:
            resposta = self.session.request(
                caso.metodo, url, headers=headers,
                data=body.encode("utf-8") if body else None,
                timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True,
            )
        except requests.RequestException as error:
            resultado.tempo_ms = int((time.perf_counter() - inicio) * 1000)
            resultado.erro = f"{aviso_vars}Falha na requisição: {error}"
            return resultado
        resultado.tempo_ms = int((time.perf_counter() - inicio) * 1000)
        self._concluir_resultado(caso, resultado, resposta, aviso_vars)
        return resultado

    def _concluir_resultado(self, caso: ApiTestCase, resultado: ApiCaseResult, resposta, aviso_vars: str = "") -> None:
        """
        Parte comum entre a execução direta (requests) e a execução externa
        (navegador): preenche a resposta no resultado, avalia as asserções e
        extrai as variáveis pros próximos casos.
        """
        resultado.status_code = resposta.status_code
        resultado.status_text = resposta.reason or ""
        resultado.response_headers = dict(resposta.headers)
        resultado.response_body = resposta.text or ""

        json_body = None
        try:
            json_body = resposta.json()
        except Exception:
            json_body = None

        for assercao in caso.assercoes:
            resultado.assercoes.append(self._avaliar(assercao, resposta, json_body, resultado.tempo_ms))
        if not caso.assercoes:
            # Sem regra nenhuma o caso não prova nada — reprova de forma
            # explícita em vez de "passar" por omissão.
            resultado.assercoes.append(ApiAssertionResult(
                "Nenhuma asserção definida", False, "adicione ao menos o status HTTP esperado",
            ))

        if aviso_vars:
            resultado.assercoes.insert(0, ApiAssertionResult(aviso_vars.strip(), False, "Defina as variáveis antes de executar."))

        for ext in caso.extrair:
            if json_body is None:
                continue
            valor = self.obter_caminho(json_body, self.substituir(ext.caminho))
            if valor is not self._AUSENTE:
                self.variaveis[ext.nome] = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)

    # ---- Execução externa (respostas obtidas fora daqui, ex.: navegador) ----
    class _RespostaExterna:
        """Mesma interface mínima de requests.Response usada em _avaliar."""
        def __init__(self, status_code, reason, headers, text):
            self.status_code = status_code
            self.reason = reason or ""
            self.headers = _HeadersCI(headers or {})
            self.text = text or ""

        def json(self):
            return json.loads(self.text)

    def avaliar_execucao_externa(self, casos: list, respostas: list) -> list:
        """
        Recebe as respostas brutas obtidas por outro executor (o componente
        de navegador), na MESMA ordem dos casos, e produz os ApiCaseResult
        exatamente como a execução direta faria — substituição de
        variáveis, asserções e extração acontecem aqui, em Python, pra que
        as duas formas de execução avaliem igual.

        respostas: [{"status", "status_text", "headers", "body", "tempo_ms",
                     "erro", "url_final", "request_headers", "request_body"}]
        Casos desabilitados não vêm nas respostas (são pulados aqui).
        """
        resultados = []
        idx = 0
        for caso in casos:
            if isinstance(caso, dict):
                caso = ApiTestCase.from_dict(caso)
            if not caso.habilitado:
                resultados.append(ApiCaseResult(
                    case_id=caso.id, nome=caso.nome, metodo=caso.metodo, url_final=caso.url,
                    request_headers={}, request_body="", status_code=None, status_text="",
                    response_headers={}, response_body="", tempo_ms=0, pulado=True,
                ))
                continue
            r = respostas[idx] if idx < len(respostas) else None
            idx += 1
            url = self.substituir(caso.url)
            headers = {k: self.substituir(v) for k, v in (caso.headers or {}).items()}
            body = self.substituir(caso.body or "")
            resultado = ApiCaseResult(
                case_id=caso.id, nome=caso.nome, metodo=caso.metodo, url_final=url,
                request_headers=headers, request_body=body, status_code=None, status_text="",
                response_headers={}, response_body="", tempo_ms=int((r or {}).get("tempo_ms") or 0),
            )
            if r is None:
                resultado.erro = "O navegador não devolveu resposta para este caso."
            elif r.get("status") is None:
                resultado.erro = f"Falha na requisição: {r.get('erro') or 'sem detalhe'}"
            else:
                resposta = self._RespostaExterna(int(r["status"]), r.get("status_text"),
                                                 r.get("response_headers") or r.get("headers") or {}, r.get("body") or "")
                self._concluir_resultado(caso, resultado, resposta)
            resultados.append(resultado)
        return resultados

    # ---- Asserções ---------------------------------------------------------
    def _avaliar(self, a: ApiAssertion, resposta, json_body, tempo_ms: int) -> ApiAssertionResult:
        tipo = a.tipo
        alvo = self.substituir(a.alvo or "")
        valor = self.substituir(a.valor or "")
        desc = a.descricao or self._descricao_padrao(tipo, alvo, valor)

        try:
            if tipo == "status":
                esperado = int(valor)
                ok = resposta.status_code == esperado
                return ApiAssertionResult(desc, ok, "" if ok else f"esperado {esperado}, obtido {resposta.status_code}")

            if tipo == "response_time_max":
                limite = int(float(valor))
                ok = tempo_ms <= limite
                return ApiAssertionResult(desc, ok, "" if ok else f"levou {tempo_ms} ms (limite {limite} ms)")

            if tipo == "header_contains":
                obtido = resposta.headers.get(alvo, "")
                ok = valor.lower() in str(obtido).lower()
                return ApiAssertionResult(desc, ok, "" if ok else f"header '{alvo}' = '{obtido}'")

            if tipo == "body_contains":
                ok = valor in (resposta.text or "")
                return ApiAssertionResult(desc, ok, "" if ok else "trecho não encontrado no corpo")

            if tipo == "body_not_contains":
                ok = valor not in (resposta.text or "")
                return ApiAssertionResult(desc, ok, "" if ok else "trecho encontrado no corpo")

            # Daqui pra baixo precisa de JSON
            if json_body is None:
                return ApiAssertionResult(desc, False, "resposta não é JSON válido")

            obtido = self.obter_caminho(json_body, alvo)
            ausente = obtido is self._AUSENTE

            if tipo == "json_exists":
                return ApiAssertionResult(desc, not ausente, "" if not ausente else f"campo '{alvo}' ausente")
            if tipo == "json_absent":
                return ApiAssertionResult(desc, ausente, "" if ausente else f"campo '{alvo}' presente: {self._resumo(obtido)}")
            if ausente:
                return ApiAssertionResult(desc, False, f"campo '{alvo}' ausente")

            if tipo == "json_not_empty":
                ok = obtido not in ("", None, [], {})
                return ApiAssertionResult(desc, ok, "" if ok else f"campo '{alvo}' vazio")
            if tipo == "json_type":
                ok = self._checar_tipo(obtido, valor)
                return ApiAssertionResult(desc, ok, "" if ok else f"tipo obtido: {type(obtido).__name__}")
            if tipo == "json_contains":
                texto = obtido if isinstance(obtido, str) else json.dumps(obtido, ensure_ascii=False)
                ok = valor in texto
                return ApiAssertionResult(desc, ok, "" if ok else f"obtido: {self._resumo(obtido)}")
            if tipo == "json_equals":
                ok = self._igual(obtido, valor)
                return ApiAssertionResult(desc, ok, "" if ok else f"esperado '{valor}', obtido {self._resumo(obtido)}")
            if tipo == "json_equals_var":
                if valor not in self.variaveis:
                    return ApiAssertionResult(desc, False, f"variável '{a.valor}' não definida (o caso que a salva rodou?)")
                ok = self._igual(obtido, self.variaveis[valor] if valor in self.variaveis else valor)
                return ApiAssertionResult(desc, ok, "" if ok else f"variável = '{self.variaveis.get(valor)}', obtido {self._resumo(obtido)}")

            return ApiAssertionResult(desc, False, f"tipo de asserção desconhecido: {tipo}")
        except Exception as error:
            return ApiAssertionResult(desc, False, f"erro ao avaliar: {error}")

    @staticmethod
    def _descricao_padrao(tipo: str, alvo: str, valor: str) -> str:
        from qa_testgen.domain.models.api_test import ASSERTION_LABELS
        rotulo = ASSERTION_LABELS.get(tipo, tipo)
        partes = [rotulo]
        if alvo:
            partes.append(f"'{alvo}'")
        if valor:
            partes.append(str(valor))
        return " ".join(partes)

    @staticmethod
    def _resumo(valor) -> str:
        texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
        return f"'{texto[:80]}{'…' if len(texto) > 80 else ''}'"

    @staticmethod
    def _igual(obtido, esperado: str) -> bool:
        if isinstance(obtido, bool):
            return str(obtido).lower() == esperado.strip().lower()
        if isinstance(obtido, (int, float)):
            try:
                return float(obtido) == float(esperado)
            except ValueError:
                return False
        if isinstance(obtido, str):
            return obtido == esperado or obtido.strip().lower() == esperado.strip().lower()
        if obtido is None:
            return esperado.strip().lower() in ("null", "none", "")
        return json.dumps(obtido, ensure_ascii=False, sort_keys=True) == esperado

    @staticmethod
    def _checar_tipo(obtido, tipo: str) -> bool:
        t = tipo.strip().lower()
        if t in ("string", "str"):
            return isinstance(obtido, str)
        if t in ("number", "int", "float", "integer"):
            return isinstance(obtido, (int, float)) and not isinstance(obtido, bool)
        if t in ("boolean", "bool"):
            return isinstance(obtido, bool)
        if t in ("array", "list"):
            return isinstance(obtido, list)
        if t in ("object", "dict"):
            return isinstance(obtido, dict)
        if t in ("null", "none"):
            return obtido is None
        return False
