"""
Regra única de retentativas pras chamadas de IA (n8n) do app — em lote
(Matriz/Casos/Planos, imagens do documento) ou chamada única (Análise,
Vínculos, Narrativa, WIQL, Manual, Testes de API):

  * entre unidades que deram certo: intervalo curto (5s nos lotes de
    geração; 0 nas demais);
  * quando uma unidade FALHA: espera a janela cheia do rate limit (62s) e
    tenta a MESMA unidade de novo, até 3 vezes — só então marca erro; e
    ainda espera 62s antes da próxima, porque a cota provavelmente
    continua estourada.

A espera nunca trava uma execução do Streamlit: cada execução dorme no
máximo ~2s e dispara st.rerun() até o relógio liberar (ver
_processar_um_lote_por_execucao pro motivo).

Mixin de UserInterface (mesmo padrão de ApiTestsPageMixin).
"""
import time

import requests
import streamlit as st


class IaRetryMixin:
    def _iniciar_geracao_em_lotes(self, action_name: str, state_prefix):
        """
        Callback dos botões "Gerar Matriz/Casos/Planos" — sempre limpa
        qualquer estado de lote (`_{state_prefix}_lotes_pendentes/acumulado/
        erros/total/proxima_liberacao/tentativas_lote_atual`) deixado por
        uma tentativa anterior ANTES de disparar a ação de novo.

        Por quê: _processar_um_lote_por_execucao usa "pendentes is None"
        pra decidir se monta lotes novos ou continua de onde parou — o que
        é certo ENQUANTO os reruns automáticos entre lotes de uma mesma
        rodada acontecem, mas quebra se a sessão cair no meio (conexão,
        timeout do navegador) e a pessoa clicar "Gerar" de novo: sem essa
        limpeza, ele silenciosamente retoma da lista velha, pulando os
        lotes já consumidos pela tentativa anterior — mesmo que ela nunca
        tenha terminado de verdade. Foi exatamente isso que fez um
        documento de 18 itens da Matriz gerar Casos só pros 2 últimos.

        BUG histórico corrigido aqui: o sufixo usado abaixo pra apagar a
        fila era '_pendentes', mas a chave DE VERDADE usada em
        _processar_um_lote_por_execucao é '_lotes_pendentes' — ou seja,
        essa limpeza NUNCA removia a fila antiga (só os outros 3 campos),
        deixando `acumulado/erros/total` zerados mas `pendentes` com uma
        lista velha, causando "None - int" (TypeError) na próxima geração
        depois de uma tentativa interrompida no meio.

        state_prefix pode ser um nome só ou uma lista/tupla (fluxos com
        mais de uma fase, ex.: imagens + análise). '_prep' é o cache de
        preparação (texto extraído, Work Items buscados) que alguns fluxos
        guardam pra não refazer o trabalho a cada rerun da espera.
        """
        self.state.set('ia_causa_visivel', None)   # aviso da rodada anterior não fica na tela
        prefixos = [state_prefix] if isinstance(state_prefix, str) else list(state_prefix)
        for prefixo in prefixos:
            for suffix in ('_lotes_pendentes', '_acumulado', '_erros', '_total', '_proxima_liberacao',
                           '_tentativas_lote_atual', '_motivo_espera', '_prep'):
                self.state.delete(f'_{prefixo}{suffix}')
        self.trigger_action(action_name)

    @staticmethod
    def _erro_lote_amigavel(error: Exception) -> str:
        """
        Mensagem de erro de 1 lote de geração (Matriz/Casos/Planos) — pra
        Timeout/ConnectionError/corpo vazio, acrescenta a causa mais comum
        num setup com n8n self-hosted atrás de proxy reverso: o proxy
        (Nginx/Nginx Proxy Manager etc.) derruba a conexão com um timeout
        PRÓPRIO, mais curto que os 300s que o app espera, antes do n8n
        terminar de chamar a IA — o app nunca chega a saber que era só
        lentidão, e trata como falha. Ver n8n_workflows/nginx_docker_timeout.md
        pra aumentar esse timeout no proxy.
        """
        msg = str(error)
        dica = (
            " 💡 Se isso se repete (principalmente em lotes maiores/documentos maiores), "
            "suspeite do timeout do proxy reverso na frente do n8n — ele pode estar "
            "encerrando a conexão antes da IA terminar de responder, mesmo dentro do "
            "limite de 300s configurado aqui. Veja n8n_workflows/nginx_docker_timeout.md."
        )
        if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return msg + dica
        if isinstance(error, ValueError) and "corpo vazio" in msg:
            return msg + dica
        return msg

    @staticmethod
    def _causa_da_falha(erro: str) -> str:
        """
        Traduz o erro CRU dos provedores de IA (que vem em inglês, dentro do
        "detalhe" do 502 do n8n) na causa real, em uma frase.

        Sem isso, a pessoa lê "Todos os provedores de IA falharam ... Request
        too large for model openai/gpt-oss-120b ... tokens per minute (TPM):
        Limit 8000" e não tem como saber que isso é cota gratuita estourada, e
        não defeito do app ou do documento dela.
        """
        texto = (erro or "").lower()
        if "tokens per minute" in texto or "request too large" in texto or "tpm" in texto:
            return ("**Cota de IA estourada (tokens por minuto).** O provedor gratuito aceita um volume limitado "
                    "por minuto e este envio passou do teto. Não é erro do seu documento nem do app — o app espera "
                    "a cota voltar e tenta de novo sozinho.")
        if "rate limit" in texto or "429" in texto or "too many requests" in texto:
            return ("**Provedor de IA em rate limit (429).** Muitas chamadas em pouco tempo na conta gratuita. "
                    "O app aguarda a janela do limite e repete o lote.")
        if "quota" in texto or "resource_exhausted" in texto or "insufficient_quota" in texto:
            return ("**Cota diária/mensal da conta de IA esgotada.** Diferente do limite por minuto, esperar não "
                    "resolve: é preciso liberar cota no provedor (ou usar outra chave).")
        if "invalid api key" in texto or "unauthorized" in texto or "401" in texto or "authentication" in texto:
            return ("**Credencial de IA inválida ou expirada no n8n.** Conferir a credencial do provedor citado no "
                    "erro, na tela de credenciais do n8n.")
        if "timeout" in texto or "timed out" in texto or "connection" in texto or "corpo vazio" in texto:
            return ("**A resposta não chegou a tempo.** Normalmente é o proxy na frente do n8n encerrando a conexão "
                    "antes da IA terminar — ou o n8n fora do ar.")
        if "model output doesn't fit" in texto or "json sem as chaves" in texto or "não é um objeto json" in texto:
            return ("**A IA respondeu fora do formato esperado.** O app já tenta os outros provedores; se todos "
                    "responderem assim, vale reduzir o tamanho do envio.")
        return ""

    # Resumo curto da causa, pra caber na label de espera do st.status.
    _RESUMO_CAUSA = (
        ("tokens per minute", "cota de IA por minuto estourada"),
        ("request too large", "cota de IA por minuto estourada"),
        ("rate limit", "provedor de IA em rate limit"),
        ("429", "provedor de IA em rate limit"),
        ("quota", "cota da conta de IA esgotada"),
        ("timeout", "a IA não respondeu a tempo"),
        ("corpo vazio", "a IA não respondeu a tempo"),
    )

    def _registrar_causa_visivel(self, rotulo: str, erro: str, resolvido: bool, detalhe_extra: str = "") -> None:
        """
        Guarda a causa da falha pra ser mostrada NA ÁREA PRINCIPAL da tela
        (ver _render_aviso_ia), não só dentro do st.status — que fica no fim
        da página, colapsado, e com o erro cru em inglês.
        """
        texto = (erro or "").lower()
        resumo = next((curto for chave, curto in self._RESUMO_CAUSA if chave in texto), "falha ao chamar a IA")
        self.state.set('ia_causa_visivel', {
            "resumo": resumo,
            "causa": self._causa_da_falha(erro),
            "onde": rotulo,
            "detalhe": (erro or "")[:1200],
            "extra": detalhe_extra,
            "resolvido": resolvido,
        })

    # Intervalo entre o FIM de um lote que deu certo e o INÍCIO do próximo,
    # na geração de Matriz/Casos/Planos. Curto de propósito: o n8n tem seis
    # provedores em fallback, então na maioria das vezes o lote seguinte
    # passa sem esperar a cota por minuto (TPM) de um provedor específico
    # se recuperar — e esperar 1 minuto entre TODOS os lotes deixava a
    # geração lenta demais quando não havia erro nenhum.
    _ESPERA_ENTRE_LOTES_SEGUNDOS = 5

    # Espera depois de um lote que FALHOU (antes de tentar o mesmo de novo
    # e antes do próximo). Alinhada à janela real do rate limit (60s):
    # erro real capturado num lote (Groq, modelo openai/gpt-oss-20b):
    # "Limit 8000, Used 2331, Requested 6285" — UM ÚNICO lote já pede
    # ~6285 tokens, quase 80% do limite de 8000/min. Se um lote falhou, é
    # sinal de que a cota já estourou; só a espera cheia resolve.
    _ESPERA_APOS_ERRO_SEGUNDOS = 62

    # Quantas vezes tenta de novo o MESMO lote antes de desistir e marcar
    # como falha de verdade. Motivo de existir: confirmado repetidas vezes
    # (inclusive com a mensagem exata "OpenAI: Rate limit reached" vinda do
    # próprio n8n) que essas falhas são rate limit passageiro dos
    # provedores de IA — na prática, tentar de novo depois da mesma espera
    # de _ESPERA_APOS_ERRO_SEGUNDOS resolve na maioria das vezes. Sem
    # isso, o app desistia na PRIMEIRA falha e jogava o problema de volta
    # pra pessoa clicar "Gerar" de novo à mão.
    _MAX_TENTATIVAS_POR_LOTE = 3

    def _processar_um_lote_por_execucao(self, state_prefix: str, montar_lotes_fn, processar_um_lote_fn, status,
                                        nome_item: str = "lote", espera_entre_lotes: float = None):
        """
        Processa SÓ 1 lote por execução do script Streamlit, disparando
        st.rerun() entre cada um — em vez de rodar um `for` com todos os
        lotes dentro da MESMA execução.

        Por quê: mesmo com cada chamada de IA individual OK (dentro do
        timeout de 300s configurado no cliente), rodar VÁRIAS chamadas
        seguidas dentro de uma única execução do script soma o tempo de
        todas elas numa única "conexão" — se existir QUALQUER limite de
        tempo entre o navegador e o servidor (proxy, load balancer, o
        próprio Streamlit Cloud), é esse tempo SOMADO que estoura o
        limite, não o de uma chamada isolada. Processando 1 lote por
        execução, cada "perna" do processo dura só o tempo de 1 chamada,
        e o rerun() devolve o controle ao navegador entre uma e outra —
        resetando qualquer relógio de conexão que exista no meio do
        caminho, fora do meu controle via código Python.

        Entre um lote e outro (a partir do 2º) também é respeitada uma
        espera — `_ESPERA_ENTRE_LOTES_SEGUNDOS` (curta) depois de um lote
        que deu certo, `_ESPERA_APOS_ERRO_SEGUNDOS` (janela cheia do rate
        limit) depois de um lote que falhou — mas SEM travar a execução
        inteira num único `time.sleep()`: em vez disso, cada execução dorme
        só uns 2s por vez e dispara rerun() de novo até o relógio liberar.
        Isso preserva o motivo do parágrafo acima (nenhuma execução
        individual fica bloqueada por muito tempo) enquanto ainda espaça as
        chamadas de verdade pro n8n.

        Um lote que FALHA tenta de novo automaticamente (até
        `_MAX_TENTATIVAS_POR_LOTE` vezes, esperando
        `_ESPERA_APOS_ERRO_SEGUNDOS` entre tentativas) antes de desistir e
        marcar como erro de verdade —
        confirmado repetidas vezes que essas falhas são rate limit
        passageiro dos provedores de IA no n8n, então esperar e tentar de
        novo resolve na maioria dos casos, sem precisar que a pessoa clique
        em "Gerar" à mão outra vez.

        montar_lotes_fn: função sem argumento, chamada só na primeira
        execução, que retorna a lista de lotes já dividida.
        processar_um_lote_fn: recebe 1 lote, retorna (lista_de_itens, erro_ou_None).

        nome_item: como chamar cada unidade nas mensagens ("lote",
        "imagem", "chamada"). espera_entre_lotes: segundos entre unidades
        que deram certo (None = _ESPERA_ENTRE_LOTES_SEGUNDOS; 0 = sem
        espera — a espera longa pós-erro vale sempre).

        Retorna (resultado_acumulado, lista_de_erros) só na execução
        FINAL (depois do último lote) — nas execuções intermediárias,
        dispara st.rerun() e a função nunca chega a retornar de verdade
        pro chamador (rerun() interrompe o script inteiro ali mesmo).
        """
        if espera_entre_lotes is None:
            espera_entre_lotes = self._ESPERA_ENTRE_LOTES_SEGUNDOS
        key_pendentes = f"_{state_prefix}_lotes_pendentes"
        key_acumulado = f"_{state_prefix}_acumulado"
        key_erros = f"_{state_prefix}_erros"
        key_total = f"_{state_prefix}_total"
        key_proxima_liberacao = f"_{state_prefix}_proxima_liberacao"
        key_tentativas = f"_{state_prefix}_tentativas_lote_atual"
        key_motivo_espera = f"_{state_prefix}_motivo_espera"   # 'erro' quando a espera é a longa, pós-falha

        if self.state.get(key_pendentes) is None:
            lotes = montar_lotes_fn()
            self.state.set(key_pendentes, lotes)
            self.state.set(key_acumulado, [])
            self.state.set(key_erros, [])
            self.state.set(key_total, len(lotes))
            self.state.set(key_proxima_liberacao, None)
            self.state.set(key_tentativas, 0)

        pendentes = self.state.get(key_pendentes)
        acumulado = self.state.get(key_acumulado)
        erros = self.state.get(key_erros)
        total = self.state.get(key_total)
        concluidos = total - len(pendentes)
        varios = total > 1
        # rótulo da unidade atual nas mensagens: "lote 2 de 5" / "imagem 3 de 8" / "a chamada à IA"
        rotulo_atual = f"{nome_item} {concluidos + 1} de {total}" if varios else (f"a {nome_item}" if nome_item != "lote" else "o lote")

        if pendentes:
            proxima_liberacao = self.state.get(key_proxima_liberacao)
            if proxima_liberacao:
                faltam = proxima_liberacao - time.time()
                if faltam > 0:
                    if self.state.get(key_motivo_espera) == 'erro':
                        quem = f"{'o' if nome_item == 'lote' else 'a'} {nome_item} anterior" if varios else "a tentativa anterior"
                        causa = (self.state.get('ia_causa_visivel') or {}).get('resumo') or ''
                        motivo = f"({quem} falhou — {causa or 'dando tempo da cota da IA no n8n se recuperar'})"
                    else:
                        motivo = f"(intervalo curto entre {nome_item}s)"
                    antes_de = f"do {rotulo_atual}" if varios else "de tentar de novo"
                    status.update(
                        label=f"Aguardando {int(faltam) + 1}s antes {antes_de} {motivo}...",
                        state="complete",  # ver nota abaixo antes do 2º st.rerun() desta função
                    )
                    time.sleep(min(faltam, 2))
                    st.rerun()
                    return None
                self.state.set(key_proxima_liberacao, None)
                self.state.set(key_motivo_espera, None)

            if varios:
                status.update(label=f"Processando {rotulo_atual}...")
            lote_atual = pendentes[0]
            itens, erro = processar_um_lote_fn(lote_atual)
            deu_erro = bool(erro)
            if erro:
                tentativas = (self.state.get(key_tentativas) or 0) + 1
                if tentativas < self._MAX_TENTATIVAS_POR_LOTE:
                    # Falha, mas ainda sobra tentativa — NÃO avança pro
                    # próximo lote: espera de novo e tenta O MESMO lote.
                    self.state.set(key_tentativas, tentativas)
                    self._registrar_causa_visivel(rotulo_atual, erro, resolvido=False,
                                                  detalhe_extra=f"tentativa {tentativas} de {self._MAX_TENTATIVAS_POR_LOTE}")
                    status.write(
                        f"⚠️ {rotulo_atual[0].upper() + rotulo_atual[1:]} falhou (tentativa {tentativas}/"
                        f"{self._MAX_TENTATIVAS_POR_LOTE}): {erro} — tentando de novo em "
                        f"{self._ESPERA_APOS_ERRO_SEGUNDOS}s..."
                    )
                    self.state.set(key_proxima_liberacao, time.time() + self._ESPERA_APOS_ERRO_SEGUNDOS)
                    self.state.set(key_motivo_espera, 'erro')
                    status.update(state="complete")
                    st.rerun()
                    return None
                erros.append((concluidos + 1, erro))
                self._registrar_causa_visivel(rotulo_atual, erro, resolvido=False,
                                              detalhe_extra=f"desistiu após {self._MAX_TENTATIVAS_POR_LOTE} tentativas")
                status.write(
                    f"❌ {rotulo_atual[0].upper() + rotulo_atual[1:]} falhou após "
                    f"{self._MAX_TENTATIVAS_POR_LOTE} tentativas: {erro}"
                )
            else:
                acumulado.extend(itens)
                if varios:
                    status.write(f"✅ {rotulo_atual[0].upper() + rotulo_atual[1:]}: {len(itens)} item(ns).")

            self.state.set(key_tentativas, 0)  # zera pro próximo lote
            novos_pendentes = pendentes[1:]
            self.state.set(key_pendentes, novos_pendentes)
            self.state.set(key_acumulado, acumulado)
            self.state.set(key_erros, erros)

            if novos_pendentes:
                # Deu certo → intervalo curto. Falhou (mesmo esgotando as
                # tentativas) → espera cheia antes do próximo, porque a cota
                # do provedor provavelmente ainda está estourada.
                espera = self._ESPERA_APOS_ERRO_SEGUNDOS if deu_erro else espera_entre_lotes
                self.state.set(key_proxima_liberacao, time.time() + espera)
                self.state.set(key_motivo_espera, 'erro' if deu_erro else None)
                # state="complete" aqui (e no rerun da espera acima) por um
                # motivo puramente cosmético do Streamlit: st.rerun() levanta
                # RerunException, que atravessa o "with st.status(...)" do
                # chamador ANTES do rerun de fato acontecer — e o __exit__ do
                # StatusContainer (streamlit/elements/lib/mutable_status_
                # container.py) força state="error" pra QUALQUER exceção em
                # trânsito enquanto o status ainda estiver "running", sem
                # distinguir uma RerunException intencional de um erro de
                # verdade. Sem isso, o card de status pisca vermelho a cada
                # lote/espera, mesmo quando está tudo certo — confirmado lendo
                # o código-fonte do Streamlit instalado no venv do projeto.
                status.update(state="complete")
                st.rerun()
                return None

        self.state.set(key_pendentes, None)
        self.state.set(key_acumulado, None)
        self.state.set(key_erros, None)
        self.state.set(key_total, None)
        self.state.set(key_proxima_liberacao, None)
        self.state.set(key_tentativas, None)
        self.state.set(key_motivo_espera, None)
        return acumulado, erros

    def _chamar_ia_com_retentativas(self, state_prefix: str, montar_payload_fn, chamar_fn, status, nome_item: str = "chamada à IA"):
        """
        Uma chamada ÚNICA de IA (Análise, Vínculos, Narrativa, WIQL, Manual,
        Testes de API) com a MESMA regra dos lotes: se falhar, espera
        `_ESPERA_APOS_ERRO_SEGUNDOS` e tenta de novo, até
        `_MAX_TENTATIVAS_POR_LOTE` vezes — sem travar a execução (é o
        _processar_um_lote_por_execucao com um "lote" só, então a espera é
        feita com reruns curtos).

        montar_payload_fn: sem argumento; roda SÓ na primeira execução (o
        payload fica guardado no estado entre os reruns da espera) — é o
        lugar certo pra preparação cara (buscar Work Items, extrair texto).
        chamar_fn: recebe o payload, devolve a resposta (dict) ou levanta.

        Retorna None nas execuções intermediárias (rerun já disparado — o
        chamador deve só dar `return`) e, na final, (payload, resposta, None)
        em caso de sucesso ou (payload, None, mensagem_de_erro) se esgotou
        as tentativas. Se a PREPARAÇÃO falhar, devolve (None, None, erro)
        na hora, sem retentativa (não é rate limit).
        """
        capturado = {}
        # A preparação roda só quando não há retentativa pendente (é quando
        # o loop chamaria montar_lotes_fn); se ela falhar, não é rate limit —
        # devolve o erro na hora, sem retentativa.
        payload_inicial = None
        if self.state.get(f"_{state_prefix}_lotes_pendentes") is None:
            try:
                payload_inicial = montar_payload_fn()
            except Exception as error:
                return None, None, str(error)

        def processar(payload):
            capturado["payload"] = payload
            try:
                return [chamar_fn(payload)], None
            except Exception as error:
                return [], self._erro_lote_amigavel(error)

        resultado = self._processar_um_lote_por_execucao(state_prefix, lambda: [payload_inicial], processar, status,
                                                          nome_item=nome_item, espera_entre_lotes=0)
        if resultado is None:
            return None
        itens, erros = resultado
        payload = capturado.get("payload")
        if erros:
            return payload, None, erros[0][1]
        return payload, itens[0], None
