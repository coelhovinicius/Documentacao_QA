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

from qa_testgen.infrastructure.api_discovery import rota_nao_encontrada
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


class CaminhoInvalido(ValueError):
    """Caminho JSON com sintaxe que o executor não entende."""


class Varios(list):
    """Resultado de caminho com [*] ou filtro: todos os valores encontrados."""


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

    # ---- Caminho JSON (subconjunto de JSONPath) -----------------------------
    _AUSENTE = object()
    _RE_FILTRO = re.compile(r"^\?\(\s*@\.([\w.\-]+)\s*(?:(==|!=)\s*(.+?))?\s*\)$")
    AJUDA_CAMINHO = ("use data.campo, lista[0], lista[*].campo, lista[?(@.campo=='valor')].campo, "
                     "lista.length ou ['campo com espaço']")

    @classmethod
    def _tokens_caminho(cls, caminho: str) -> list:
        """
        Quebra o caminho em passos. Aceita o que a IA e o Postman costumam
        escrever: `$.data.x`, `a[0]`, `a[-1]`, `a[*]`, `a.*`, `a['chave']`,
        `a[?(@.nome=='X')]`, `a[?(@.id==7)]`, `a[?(@.ativo)]`, `a.length`.
        Sintaxe fora disso levanta CaminhoInvalido (vira mensagem clara na
        asserção, em vez de "campo ausente").
        """
        s = (caminho or "").strip()
        if s.startswith("$"):
            s = s[1:]
        tokens, i = [], 0
        while i < len(s):
            c = s[i]
            if c == ".":
                if s[i:i + 2] == "..":
                    raise CaminhoInvalido("busca recursiva '..' não é suportada")
                i += 1
                continue
            if c == "[":
                j, prof, aspas = i + 1, 0, None
                while j < len(s):
                    ch = s[j]
                    if aspas:
                        aspas = None if ch == aspas else aspas
                    elif ch in "'\"":
                        aspas = ch
                    elif ch in "([":
                        prof += 1
                    elif ch in ")]":
                        if ch == "]" and prof == 0:
                            break
                        prof -= 1
                    j += 1
                if j >= len(s):
                    raise CaminhoInvalido("colchete '[' sem fechar")
                dentro = s[i + 1:j].strip()
                if dentro == "*":
                    tokens.append(("*",))
                elif re.fullmatch(r"-?\d+", dentro):
                    tokens.append(("idx", int(dentro)))
                elif len(dentro) >= 2 and dentro[0] in "'\"" and dentro[-1] == dentro[0]:
                    tokens.append(("key", dentro[1:-1]))
                elif dentro.startswith("?"):
                    m = cls._RE_FILTRO.match(dentro)
                    if not m:
                        raise CaminhoInvalido(f"filtro '[{dentro}]' fora do formato [?(@.campo=='valor')]")
                    tokens.append(("filtro", m.group(1), m.group(2), cls._valor_filtro(m.group(3))))
                else:
                    raise CaminhoInvalido(f"'[{dentro}]' não é índice, [*], ['chave'] nem filtro")
                i = j + 1
                continue
            j = i
            while j < len(s) and s[j] not in ".[":
                j += 1
            nome = s[i:j]
            if nome == "*":
                tokens.append(("*",))
            elif nome == "length()":
                tokens.append(("len",))
            else:
                tokens.append(("key", nome))
            i = j
        return tokens

    @staticmethod
    def _valor_filtro(bruto):
        if bruto is None:
            return None
        v = bruto.strip()
        if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
            return v[1:-1]
        if v.lower() in ("true", "false"):
            return v.lower() == "true"
        if v.lower() == "null":
            return None
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                raise CaminhoInvalido(f"valor '{v}' do filtro precisa estar entre aspas (ou ser número)")

    @classmethod
    def _filtro_ok(cls, item, campo: str, op, valor) -> bool:
        obtido = cls.obter_caminho(item, campo)
        if obtido is cls._AUSENTE:
            return False
        if op is None:
            return bool(obtido)
        igual = obtido == valor or (isinstance(obtido, (int, float)) and isinstance(valor, (int, float)) and float(obtido) == float(valor)) \
            or str(obtido) == str(valor)
        return igual if op == "==" else not igual

    @classmethod
    def obter_caminho(cls, dado, caminho: str):
        """
        `data.user.email`, `errors.email[0]`, `items[2].id`, e também
        `items[*].id`, `items[?(@.nome=='X')].id` e `items.length`.
        Retorna cls._AUSENTE quando o caminho não existe; com `[*]` ou
        filtro devolve Varios (lista dos valores encontrados, podendo ser
        vazia). Sintaxe não suportada levanta CaminhoInvalido.
        """
        if not caminho:
            return dado
        atuais, varios = [dado], False
        for tok in cls._tokens_caminho(caminho):
            prox = []
            for atual in atuais:
                if tok[0] == "key":
                    if isinstance(atual, dict) and tok[1] in atual:
                        prox.append(atual[tok[1]])
                    elif tok[1] == "length" and isinstance(atual, (list, str)):
                        prox.append(len(atual))
                elif tok[0] == "len":
                    if isinstance(atual, (list, str, dict)):
                        prox.append(len(atual))
                elif tok[0] == "idx":
                    if isinstance(atual, list) and -len(atual) <= tok[1] < len(atual):
                        prox.append(atual[tok[1]])
                elif tok[0] == "*":
                    varios = True
                    if isinstance(atual, list):
                        prox.extend(atual)
                    elif isinstance(atual, dict):
                        prox.extend(atual.values())
                elif tok[0] == "filtro":
                    varios = True
                    if isinstance(atual, list):
                        prox.extend(item for item in atual if cls._filtro_ok(item, tok[1], tok[2], tok[3]))
            atuais = prox
            if not atuais and not varios:
                return cls._AUSENTE
        if varios:
            return Varios(atuais)
        return atuais[0] if atuais else cls._AUSENTE

    # ---- Execução ----------------------------------------------------------
    def executar(self, casos: list, on_progress: Optional[Callable[[int, int, ApiCaseResult], None]] = None) -> list:
        resultados = []
        total = len(casos)
        produtores = {}   # variável -> nome do caso anterior que deveria extraí-la
        for idx, caso in enumerate(casos, start=1):
            if isinstance(caso, dict):
                caso = ApiTestCase.from_dict(caso)
            if not caso.habilitado:
                res = self._resultado_pulado(caso)
            else:
                motivo = self.motivo_bloqueio(caso, produtores)
                res = self._resultado_pulado(caso, motivo) if motivo else self.executar_caso(caso)
            self._registrar_produtores(caso, produtores)
            resultados.append(res)
            if on_progress:
                on_progress(idx, total, res)
        return resultados

    # ---- Dependência entre casos -------------------------------------------
    @staticmethod
    def _resultado_pulado(caso: ApiTestCase, motivo: str = "") -> ApiCaseResult:
        return ApiCaseResult(
            case_id=caso.id, nome=caso.nome, metodo=caso.metodo, url_final=caso.url,
            request_headers={}, request_body="", status_code=None, status_text="",
            response_headers={}, response_body="", tempo_ms=0, pulado=True, motivo_pulo=motivo,
        )

    @staticmethod
    def _registrar_produtores(caso: ApiTestCase, produtores: dict) -> None:
        for ex in (caso.extrair or []):
            nome = getattr(ex, "nome", None) if not isinstance(ex, dict) else ex.get("nome")
            if nome and nome not in produtores:
                produtores[nome] = caso.nome

    def motivo_bloqueio(self, caso: ApiTestCase, produtores: dict) -> str:
        """
        Um caso que usa `{{variavel}}` que um caso ANTERIOR deveria ter
        extraído — e que continua vazia (a extração falhou) — não pode
        rodar de verdade: iria com a URL quebrada (ex.: /surveys//questions)
        e reprovaria por um motivo que não é dele. Devolve o motivo do
        bloqueio, ou "" se pode executar.
        """
        usadas = set(self._RE_VAR.findall(caso.url or ""))
        for v in (caso.headers or {}).values():
            usadas.update(self._RE_VAR.findall(v or ""))
        usadas.update(self._RE_VAR.findall(caso.body or ""))
        vazias = sorted(n for n in usadas if not (self.variaveis.get(n) or "").strip())
        if not vazias:
            return ""
        # Variável sem valor que nenhum caso anterior produz (preenchimento
        # manual pendente) também bloqueia só os casos que a usam — o resto da
        # bateria roda, em vez de a execução inteira esperar por ela.
        return "Bloqueado: " + "; ".join(
            f"a variável '{n}' está vazia — o caso \"{produtores[n]}\" deveria extraí-la e não conseguiu" if n in produtores
            else f"a variável '{n}' está sem valor (preencha na seção Variáveis, ou um caso anterior precisa extraí-la)"
            for n in vazias
        ) + ". Corrija e execute de novo."

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
        if rota_nao_encontrada(resposta.status_code, resultado.response_body):
            # A API disse que a ROTA não existe: o problema é a definição do
            # caso (rota presumida), não o comportamento da API — sai como
            # Erro, com o motivo, em vez de "Reprovado" enganoso.
            resultado.erro = (f"{aviso_vars}Rota inexistente: a API respondeu que {caso.metodo} {resultado.url_final} não existe "
                              "(404 \"route could not be found\"). Confira o catálogo de rotas reais na etapa 1.")
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
            try:
                valor = self.obter_caminho(json_body, self.substituir(ext.caminho))
            except CaminhoInvalido:
                continue
            if isinstance(valor, Varios):
                valor = valor[0] if valor else self._AUSENTE
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
        produtores = {}
        for caso in casos:
            if isinstance(caso, dict):
                caso = ApiTestCase.from_dict(caso)
            if not caso.habilitado:
                resultados.append(self._resultado_pulado(caso))
                self._registrar_produtores(caso, produtores)
                continue
            r = respostas[idx] if idx < len(respostas) else None
            idx += 1
            motivo = self.motivo_bloqueio(caso, produtores)
            if motivo or (r or {}).get("bloqueado"):
                # O navegador também pula o caso (sem mandar a requisição);
                # o motivo é recalculado aqui pra ficar igual nos dois modos.
                resultados.append(self._resultado_pulado(caso, motivo or str(r.get("bloqueado"))))
                self._registrar_produtores(caso, produtores)
                continue
            self._registrar_produtores(caso, produtores)
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

            try:
                obtido = self.obter_caminho(json_body, alvo)
            except CaminhoInvalido as error:
                # Não é "campo ausente": o executor não entendeu o caminho —
                # dizer "ausente" aqui faria a evidência mentir sobre a API.
                return ApiAssertionResult(desc, False, f"sintaxe de caminho não suportada em '{alvo}' ({error}) — {self.AJUDA_CAMINHO}")
            # Com [*]/filtro vêm vários valores: basta UM atender (tipo: todos).
            valores = list(obtido) if isinstance(obtido, Varios) else ([] if obtido is self._AUSENTE else [obtido])
            ausente = not valores

            if tipo == "json_exists":
                return ApiAssertionResult(desc, not ausente, "" if not ausente else f"campo '{alvo}' ausente")
            if tipo == "json_absent":
                return ApiAssertionResult(desc, ausente, "" if ausente else f"campo '{alvo}' presente: {self._resumo(obtido)}")
            if ausente:
                return ApiAssertionResult(desc, False, f"campo '{alvo}' ausente")

            if tipo == "json_not_empty":
                ok = any(v not in ("", None, [], {}) for v in valores)
                return ApiAssertionResult(desc, ok, "" if ok else f"campo '{alvo}' vazio")
            if tipo == "json_type":
                ok = all(self._checar_tipo(v, valor) for v in valores)
                return ApiAssertionResult(desc, ok, "" if ok else f"tipo obtido: {', '.join(sorted({type(v).__name__ for v in valores}))}")
            if tipo == "json_contains":
                ok = any(valor in (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for v in valores)
                return ApiAssertionResult(desc, ok, "" if ok else f"obtido: {self._resumo(obtido)}")
            if tipo == "json_equals":
                ok = any(self._igual(v, valor) for v in valores)
                return ApiAssertionResult(desc, ok, "" if ok else f"esperado '{valor}', obtido {self._resumo(obtido)}")
            if tipo == "json_equals_var":
                if valor not in self.variaveis:
                    return ApiAssertionResult(desc, False, f"variável '{a.valor}' não definida (o caso que a salva rodou?)")
                ok = any(self._igual(v, self.variaveis[valor]) for v in valores)
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
