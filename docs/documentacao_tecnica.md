# QA Automation – Azure DevOps
## Documentação Técnica Completa

**Projeto:** Refuturiza — QA TestGen / QA Automation
**Última atualização:** Julho de 2026

---

## 1. Visão Geral

O **QA Automation – Azure DevOps** é uma aplicação web interna que usa Inteligência Artificial para automatizar a documentação de QA de um projeto de software, do início ao fim:

1. Recebe um ou mais documentos de requisitos (PDF, DOCX ou TXT)
2. Usa IA (via n8n) para gerar, em sequência: dúvidas de negócio → Matriz de Cobertura → Casos de Teste → Planos de Teste
3. Permite revisão e edição manual de tudo antes de finalizar (CRUD completo em cada etapa)
4. Exporta CSV e PDF prontos para uso
5. Integra diretamente com o **Azure DevOps** via API — cria Test Cases, Test Plans, Requirement-based Suites, e vincula tudo automaticamente (com sugestão de vínculos via IA)

A aplicação é usada publicamente via navegador, protegida por login.

---

## 2. Arquitetura Geral

```
┌─────────────────┐        ┌──────────────────┐        ┌───────────────────┐
│   Usuário        │ ────▶  │  Streamlit Cloud  │ ────▶  │   n8n (self-hosted)│
│  (navegador)     │        │  (app Python)     │        │  Docker + nginx    │
└─────────────────┘        └──────────────────┘        │  + DuckDNS          │
                                     │                    └───────────────────┘
                                     │                              │
                                     │                              ▼
                                     │                    ┌───────────────────┐
                                     │                    │  Provedores de IA  │
                                     │                    │  Gemini / Groq /   │
                                     │                    │  OpenAI / Mistral  │
                                     │                    └───────────────────┘
                                     ▼
                           ┌───────────────────┐
                           │   Azure DevOps     │
                           │   (REST API)       │
                           └───────────────────┘
```

- **Frontend + Backend**: uma única aplicação **Streamlit** (Python), hospedada no **Streamlit Community Cloud**
- **Orquestração de IA**: **n8n**, hospedado separadamente (infraestrutura própria, via Docker + nginx + DuckDNS)
- **Repositório de código**: **GitHub**, conectado ao Streamlit Cloud para deploy automático a cada `git push`
- **Integração final**: **Azure DevOps REST API** (organização `refuturiza`)

---

## 3. Hospedagem e Deploy

### 3.1 Streamlit Cloud (a aplicação em si)

- URL pública: `https://quality-assurance-docs.streamlit.app`
- O Streamlit Cloud está conectado a um repositório **GitHub**; qualquer `git push` no branch configurado (normalmente `main`) dispara um **redeploy automático**
- As credenciais e configurações sensíveis (tokens, senhas, URLs de webhook) **não vão para o Git** — ficam nos **Secrets** do Streamlit Cloud (`App → Settings → Secrets`), no mesmo formato do `secrets.toml` local
- **Importante**: alterações feitas nos arquivos locais só valem em produção depois de `git add` → `git commit` → `git push`. O Streamlit Cloud não sincroniza sozinho com edições locais

### 3.2 Ambiente local (desenvolvimento)

- Rodado localmente via `streamlit run app.py`, num ambiente virtual Python (`.venv`)
- Usa um arquivo `.streamlit/secrets.toml`, na raiz do projeto (mesmo nível do `app.py`), **nunca commitado no Git** (deve estar no `.gitignore`)
- Esse arquivo local e os Secrets do Streamlit Cloud devem ter, essencialmente, o mesmo conteúdo — exceto quando se quer usar ambientes/tokens diferentes entre local e produção (ex.: sandbox vs. produção do Azure DevOps)

### 3.3 Git / GitHub

- Controle de versão via Git, hospedado no GitHub
- Arquivos sensíveis (`secrets.toml`, tokens, etc.) ficam fora do controle de versão via `.gitignore`
- Fluxo de trabalho: editar localmente → testar (`streamlit run app.py`) → commitar com mensagens descritivas → `git push` → Streamlit Cloud reimplanta automaticamente

---

## 4. n8n (Orquestração de IA)

### 4.1 Infraestrutura do n8n

- Hospedado via **Docker**, em infraestrutura própria (não é n8n Cloud)
- Exposto publicamente através de um domínio dinâmico via **DuckDNS** (ex.: `vinitestes-qa.duckdns.org`)
- **nginx** atua como reverse proxy na frente do container do n8n
- Autenticado via **Header Auth** (`x-api-key`), configurado tanto no lado do n8n quanto enviado pelo app Python a cada requisição

### 4.2 Workflows existentes

O app depende de **5 workflows** publicados e **ativos** no n8n:

| Workflow | Endpoint (webhook) | Função |
|---|---|---|
| `Doc_QA_Analysis` | `/webhook/qa-testgen-analysis` | Recebe o texto do(s) documento(s) e devolve uma lista de dúvidas/ambiguidades de negócio |
| `Doc_QA_Matrix` | `/webhook/qa-testgen-matrix` | Gera a Matriz de Cobertura (linhas MC-001, MC-002...) |
| `Doc_QA_Generation` | `/webhook/qa-testgen-generation` | Gera os Casos de Teste (com rastreabilidade — `requisitos_relacionados` apontando pra Matriz) |
| `Doc_QA_Plans` | `/webhook/qa-testgen-plans` | Gera os Planos de Teste (Planos → Suites → Casos) |
| `Doc_QA_Matching` | `/webhook/qa-testgen-matching` | Sugere automaticamente o vínculo entre Casos de Teste gerados e Work Items existentes no Azure DevOps |

### 4.3 Padrão interno de cada workflow

Todos os 5 workflows seguem a mesma estrutura, por resiliência:

```
Webhook → Gemini Chain → (erro) → Groq Chain → (erro) → OpenAI Chain
                                                              │
                                                          (erro)
                                                              ▼
                                            Groq Chain (2ª tentativa) → (erro) → Mistral Chain
                                                                                       │
                                                                                       ▼
                                                                          Respond to Webhook
```

- **Cadeia de fallback entre 5 provedores de IA**: se um provedor falhar (erro, schema inválido, timeout), o n8n tenta automaticamente o próximo, na ordem acima
- Cada chain node usa um **Structured Output Parser**, com schema JSON definido e campos marcados como `required` — isso força o modelo a devolver exatamente o formato esperado, e rejeita respostas incompletas (acionando o fallback)
- **Modelos configurados atualmente**:
  - Gemini: `gemini-3.5-flash` (rápido; requer chave paga para acesso — chave gratuita pode não ter esse modelo liberado, causando timeout/travamento — nesse caso, usar `gemini-2.5-flash`)
  - Groq: `llama-3.3-70b-versatile` (usado 2x na cadeia, em posições diferentes)
  - OpenAI: `gpt-5.4-mini` (recomendado — `gpt-4o` foi descontinuado pela OpenAI em 2026)
  - Mistral: `mistral-small-latest`

### 4.4 Contratos de dados (payloads)

Todas as chamadas do app para o n8n são `POST`, com corpo JSON. Listas/objetos complexos (documento, matriz, casos, etc.) são enviados como **strings JSON** dentro dos campos (não como JSON aninhado nativo), e cada workflow faz o parse internamente no prompt.

**Exemplo — `Doc_QA_Matching`:**
```json
{
  "work_items": "[{\"id\": 123, \"title\": \"...\", \"type\": \"User Story\", \"state\": \"Active\"}]",
  "casos_de_teste": "[{\"titulo\": \"...\", \"pre_condicoes\": \"...\", \"passos\": [...]}]",
  "nome_projeto": "..."
}
```
**Resposta esperada:**
```json
{
  "vinculos": [
    {"work_item_id": "123", "casos": ["Título do Caso 1", "Título do Caso 2"]}
  ]
}
```

### 4.5 Manutenção do n8n

- Cada workflow precisa estar **ativado** (toggle "Active") para que a Production URL exista — se estiver desativado, o app recebe erro de conexão
- Chaves de API dos provedores de IA (Gemini, Groq, OpenAI, Mistral) são credenciais configuradas dentro do próprio n8n, não no app
- Para depurar lentidão ou falhas: **n8n → workflow → Executions** → abrir a execução → o diagrama mostra visualmente qual(is) provedor(es) foram acionados até o sucesso (ou falha)

---

## 5. Integração com Azure DevOps

### 5.1 Autenticação

- Feita via **Personal Access Token (PAT)**, com Basic Auth (usuário vazio + PAT em Base64)
- Escopos necessários no PAT: **Work Items (Read & Write)** e **Test Management (Read & Write)**
- O PAT é configurado **apenas no backend** (`secrets.toml` / Secrets do Streamlit Cloud) — nunca é digitado, exibido ou editável na interface, por segurança
- **PATs expiram** — é preciso renovar manualmente antes do vencimento (a data é definida na criação do token, em `https://dev.azure.com/{org}/_usersSettings/tokens`)

### 5.2 Seleção de Organização / Projeto / Area Path

Todos os três níveis são obtidos **dinamicamente da API do Azure DevOps**, nunca digitados livremente:

1. **Organização**: carregada automaticamente ao abrir o Passo 7, via API de perfil (`app.vssps.visualstudio.com`). Se o PAT for restrito a uma única organização (não tem escopo "All accessible organizations"), o app cai automaticamente num fallback usando a organização configurada no `secrets.toml` como única opção
2. **Projeto**: buscado sob demanda (botão "Buscar Projetos desta Organização") — lista só os projetos que o PAT consegue visualizar
3. **Area Path**: usa por padrão a **raiz do projeto** (sempre válida, sem chamada extra); uma sub-area específica pode ser escolhida via um expansor opcional, que busca a árvore completa de Areas do projeto (`_apis/wit/classificationnodes/Areas`)

### 5.3 O que a integração cria no Azure DevOps

Para cada análise, o Passo 7 do app permite:

1. **Buscar os Work Items existentes** no Area Path selecionado (exclui automaticamente os tipos `Test Case`, `Test Plan`, `Test Suite`, e os estados `Backlog`/`Finalizado`)
2. **Sugerir vínculos automaticamente**, via IA (workflow `Doc_QA_Matching`), entre os Casos de Teste gerados e os Work Items encontrados
3. **Revisar/ajustar manualmente** os vínculos sugeridos (multiselect por Work Item — um caso pode ser vinculado a mais de um Work Item)
4. **Confirmar a integração**, que executa, nessa ordem:
   - Checa se já existe um Test Plan com o nome escolhido (bloqueia se sim)
   - Cria todos os Casos de Teste ainda não existentes no Azure DevOps (como Work Items do tipo `Test Case`), em paralelo (até 4 chamadas simultâneas)
   - Cria o Test Plan
   - Cria uma **Requirement-based Suite** por Work Item que tenha pelo menos 1 caso vinculado (sequencial — suites concorrentes geram erro de conflito de escrita no Azure DevOps)
   - Cria o vínculo de "Tests" entre cada Caso de Teste e seu(s) Work Item(s), em paralelo entre casos diferentes (mas sequencial quando é o mesmo caso vinculado a múltiplos Work Items, pra evitar escrita concorrente no mesmo item)

### 5.4 Campos e particularidades do Azure DevOps usados

- **Pré-condições**: gravadas num campo **customizado** do processo da organização (`Custom.Precondicoes`) — não é o campo padrão `System.Description`. Esse nome de campo é específico dessa organização; se reaproveitado em outra, é preciso confirmar o nome real (script `list_test_case_fields.py` ajuda a descobrir)
- **Passos do Caso de Teste**: gravados no campo `Microsoft.VSTS.TCM.Steps`, que exige um XML específico (steps com `parameterizedString` em HTML escapado)
- **Numeração dos títulos**: todo Caso de Teste ganha prefixo `CT01 -`, `CT02 -`, etc., tanto nos CSVs quanto na integração direta
- **Estado do Caso de Teste**: o Azure DevOps **não permite definir um estado não-padrão na criação** do work item (é uma regra de workflow, não uma falha do app) — por isso, o Caso é criado primeiro no estado padrão (`Design`), e depois, numa chamada separada, é feita a transição pro estado desejado (ex.: `Ready`). Se essa segunda etapa falhar, o caso continua existindo normalmente, só fica registrado um aviso
- **Rastreabilidade**: cada Caso de Teste carrega um campo `requisitos_relacionados`, com os IDs da Matriz de Cobertura (ex.: `MC-001`) que ele cobre — usado no PDF pra gerar o "Resumo de Rastreabilidade" com destaque automático pra requisitos sem nenhuma cobertura

### 5.5 Robustez técnica da integração

- **Conexão reutilizável**: `requests.Session()` com pool de conexões, em vez de abrir uma conexão nova a cada chamada
- **Retry automático**: até 4 tentativas com espera crescente, pra qualquer falha transitória de conexão (reset, timeout, erros 429/500/502/503/504)
- **Paralelismo controlado**: até 4 chamadas simultâneas via `ThreadPoolExecutor` — ajustado depois de testes reais mostrarem que 8 simultâneas causava reset de conexão

---

## 6. Autenticação e Sessão do App

- Login com usuário/senha, senhas armazenadas como **hash bcrypt** (nunca texto puro) em `st.secrets["credentials"]["usernames"]`
- Sessão mantida via um **token assinado (HMAC) guardado como parâmetro na própria URL** (`?auth=...`) — não usa cookies, evitando problemas de cookies bloqueados/isolados em iframes de componentes de terceiros
- **Logout automático por inatividade**: 60 minutos sem interação invalida o token (configurável via `INACTIVITY_TIMEOUT_MINUTES` em `auth.py`)
- Um botão "Sair" fixo no rodapé da sidebar encerra a sessão manualmente

---

## 7. Estrutura de Pastas e Arquivos

```
projeto/
├── app.py                              # ponto de entrada
├── .streamlit/
│   └── secrets.toml                    # credenciais locais (NUNCA no Git)
├── qa_testgen/
│   ├── config/
│   │   ├── constants.py                # cores, caminhos de logo, timezone
│   │   └── settings.py                 # AppConfiguration (lê st.secrets)
│   ├── ui/
│   │   ├── application.py              # UI principal (fluxo de 7 passos)
│   │   ├── auth.py                     # login/sessão
│   │   └── dialogs.py                  # modais de confirmação
│   ├── application/
│   │   └── session.py                  # wrapper do st.session_state
│   ├── domain/
│   │   ├── models/                     # MatrixRow, TestCase, TestPlan, TestStep
│   │   └── validators/                 # validação de campos obrigatórios
│   └── infrastructure/
│       ├── webhook_client.py           # chamadas aos 5 webhooks do n8n
│       ├── azure_devops_client.py      # cliente da API do Azure DevOps
│       ├── csv_formatter.py            # exportação CSV (Azure DevOps import)
│       ├── document_processor.py       # extração de texto (PDF/DOCX/TXT)
│       └── pdf_report.py               # geração do relatório PDF
└── (scripts auxiliares de teste/diagnóstico, fora do fluxo principal do app)
```

---

## 8. Configuração (`secrets.toml`)

```toml
# --- n8n ---
N8N_WEBHOOK_URL_ANALYSIS = "http://seu-n8n/webhook/qa-testgen-analysis"
N8N_WEBHOOK_URL_MATRIX = "http://seu-n8n/webhook/qa-testgen-matrix"
N8N_WEBHOOK_URL_GENERATION = "http://seu-n8n/webhook/qa-testgen-generation"
N8N_WEBHOOK_URL_PLANS = "http://seu-n8n/webhook/qa-testgen-plans"
N8N_WEBHOOK_URL_MATCHING = "http://seu-n8n/webhook/qa-testgen-matching"
N8N_API_KEY = "..."

# --- Azure DevOps (Organização/PAT como padrão; Projeto é opcional/dinâmico) ---
AZURE_DEVOPS_ORG = "refuturiza"
AZURE_DEVOPS_PROJECT = ""
AZURE_DEVOPS_PAT = "..."

[credentials]
cookie_secret = "string aleatória longa — assina o token de sessão"

[credentials.usernames]
admin = "$2b$12$....hash-bcrypt...."
```

⚠️ **Atenção à ordem no TOML**: tudo que vem depois de um cabeçalho `[tabela]` pertence a ela até aparecer outro cabeçalho — por isso as chaves "soltas" (webhooks, Azure DevOps) ficam sempre **antes** de qualquer `[tabela]` no arquivo.

---

## 9. Fluxo da Aplicação (7 Passos)

| Passo | Nome | O que acontece |
|---|---|---|
| 1 | Upload | Nome do projeto + upload de 1 ou mais documentos (PDF/DOCX/TXT, até 20MB cada e 20MB no total) |
| 2 | Dúvidas | IA identifica ambiguidades no documento; usuário responde |
| 3 | Matriz | Matriz de Cobertura gerada por IA; CRUD completo |
| 4 | Casos | Casos de Teste gerados por IA (com rastreabilidade); CRUD completo |
| 5 | Planos | Planos/Suites de Teste gerados por IA; CRUD completo |
| 6 | Download | Exporta CSV (Casos / Planos+Suites+Casos) e PDF completo |
| 7 | Azure DevOps | Configuração dinâmica (Org/Projeto/Area) → Work Items → sugestão de vínculos por IA → revisão manual → integração real |

---

## 10. Pontos de Atenção / Manutenção

- **PAT do Azure DevOps expira** — verificar a validade periodicamente
- **Modelos de IA no n8n** podem ser descontinuados (ex.: `gpt-4o` foi aposentado em 2026) — vale checar de tempos em tempos se os modelos configurados continuam ativos
- **Workflows do n8n precisam estar ativos** (`Active`) — se desativados, os webhooks somem
- **Campo `Custom.Precondicoes`** é específico da organização `refuturiza` — reaproveitar essa integração em outra organização/processo do Azure DevOps exige confirmar o nome real do campo
- **`secrets.toml` não sobe pro Git** — precisa ser configurado manualmente em cada ambiente (local + Streamlit Cloud)