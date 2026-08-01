# 🧪 QA Automation – Azure DevOps

Aplicação Streamlit que automatiza a geração de documentação de QA (Matriz de Cobertura, Casos de Teste e Planos de Teste) a partir de documentos de requisitos ou de Work Items do Azure DevOps, usando IA (via n8n), e integra tudo diretamente com o Azure DevOps — com controle de acesso, PAT pessoal por usuário e relatórios de execução baseados em dados reais do board.

Projeto da **Refuturiza**.

---

## Sumário

- [Visão geral](#visão-geral)
- [Arquitetura](#arquitetura)
- [Funcionalidades](#funcionalidades)
  - [O assistente de 7 passos](#o-assistente-de-7-passos)
  - [Recursos adicionais (sidebar)](#recursos-adicionais-sidebar)
  - [Controle de acesso e governança](#controle-de-acesso-e-governança)
- [Stack técnica](#stack-técnica)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Configuração](#configuração)
  - [Dependências Python](#dependências-python)
  - [`secrets.toml`](#secretstoml)
  - [Workflows do n8n](#workflows-do-n8n)
- [Como rodar localmente](#como-rodar-localmente)
- [Deploy](#deploy)
- [Conceitos importantes](#conceitos-importantes)
  - [Ambiente (Homologação/Produção)](#ambiente-homologaçãoprodução)
  - [PAT pessoal](#pat-pessoal)
  - [Permissões granulares](#permissões-granulares)
  - [Status de QA via coluna do board](#status-de-qa-via-coluna-do-board)
- [Limitações conhecidas](#limitações-conhecidas)
- [Manutenção](#manutenção)

---

## Visão geral

O app resolve um problema recorrente de QA: transformar uma especificação (documento, ou Work Items já existentes no Azure DevOps) em documentação de teste estruturada — Matriz de Cobertura, Casos de Teste e Planos de Teste — e publicar tudo isso diretamente no Azure DevOps, mantendo rastreabilidade, evitando duplicidade, e registrando quem fez o quê.

A geração de conteúdo usa IA (5 provedores em cadeia de fallback: Gemini → Groq → OpenAI → Groq → Mistral, dependendo do workflow) orquestrada via **n8n**, que é o único lugar onde as chaves de API de IA ficam configuradas — o app Streamlit nunca lida com essas chaves diretamente, só troca dados com o n8n por webhook.

---

## Arquitetura

```
 Usuário (Time de QA)
        │
        ▼
 Login (aprovação + PAT pessoal)
        │
        ▼
 App QA Automation (Streamlit)
        │
        ▼
 n8n  (IA + Controle de Acesso/Logs)
        │
        ▼
 Azure DevOps (Board, Test Plans, Queries)
```

- O app nunca fala direto com nenhum provedor de IA — tudo passa pelo n8n, que decide qual provedor usar e faz o fallback entre eles se um falhar.
- O n8n também guarda o **controle de acesso** (aprovadores, permissões, logs de auditoria) usando o recurso de *workflow static data* — não precisa de banco de dados separado.
- A integração com o **Azure DevOps** é feita direto do app, usando o **PAT pessoal** de quem está logado (não uma chave compartilhada).

---

## Funcionalidades

### O assistente de 7 passos

| Passo | O que faz |
|---|---|
| **1. Upload** | Envio de documento(s) (PDF/DOCX/TXT) **ou** geração a partir de Work Items existentes no Azure DevOps (ver [Gerar a partir de Work Items](#recursos-adicionais-sidebar)). Extrai texto e também **imagens do corpo do documento** (ignora cabeçalho/rodapé, ícones pequenos, logos repetidos), interpretando cada uma via IA e injetando a descrição no texto antes da análise. Exige escolher o **Ambiente** (Homologação/Produção) antes de prosseguir. |
| **2. Dúvidas** | A IA faz até 7 perguntas de esclarecimento sobre a especificação, priorizadas por impacto. |
| **3. Matriz** | Gera a Matriz de Cobertura (MC-001, MC-002...) com base no documento + respostas do Passo 2. IDs recebem a etiqueta do Ambiente escolhido (`MC-001 HML` / `MC-001 PROD`). Editável (adicionar/editar/remover linhas). |
| **4. Casos** | Gera Casos de Teste a partir da Matriz, cada um com `requisitos_relacionados` apontando pra 1+ linha da Matriz. Editável. |
| **5. Planos** | Organiza os Casos em Planos → Suítes, cobertura obrigatória (todo caso aparece em exatamente 1 suíte). Editável. |
| **6. Download** | Exporta CSV (Casos; Planos+Suítes+Casos) e PDF "Documentação QA" (Matriz, Planos, Casos, com rastreabilidade e indicação de Ambiente). |
| **7. Azure DevOps** | Integração completa: busca Organização/Projeto/Area Path pelo PAT pessoal, busca Work Items do board, sugere vínculos Caso↔Work Item via IA, e publica no Azure DevOps — cria Test Cases, cria ou **reaproveita** um Test Plan existente na Area Path (sem duplicar Suítes já existentes), e vincula tudo. |

### Recursos adicionais (sidebar)

Não fazem parte da sequência dos 7 passos — ficam sempre disponíveis na barra lateral, cada um liberado só pra quem tem a permissão certa.

- **🎯 Gerar a partir de Work Items** — em vez de enviar um documento, escolhe Work Items existentes no Azure DevOps; a Descrição + Critérios de Aceite deles viram a especificação de entrada, e o resto do fluxo (Dúvidas → Matriz → Casos → Planos) segue normal.
- **🔎 Criar Query com IA** — descreve em português o que quer consultar no Azure DevOps; a IA traduz pra WIQL, mostra um preview real dos Work Items retornados (não só a contagem) antes de qualquer coisa ser salva, e só cria a query de verdade após confirmação explícita.
- **📊 Relatório de Testes** — documenta o que foi **executado** (diferente do PDF do Passo 6, que documenta o que foi planejado). Busca um ou mais Test Plans, calcula o Status por **coluna do board (Kanban)** do Work Item vinculado a cada caso (não pelo outcome do Test Point — ver [Status de QA via coluna do board](#status-de-qa-via-coluna-do-board)), busca evidências (imagens anexadas ao Work Item do Caso de Teste), e sugere Contexto/Escopo/Conclusão/Próximos Passos via IA com base nas descrições reais dos Work Items testados. Status final do relatório é escolhido manualmente (Aprovado/Cancelado/Pendente). Monta uma Matriz de Cobertura **independente** a partir dos Work Items vinculados quando a sessão não tem uma.
- **🛡️ Administração** *(só o dono do app)* — cadastro de aprovadores de login, permissões granulares por área, e **Logs de Auditoria** (últimos 500 eventos: login, aprovações, integrações, relatórios gerados, permissões concedidas/revogadas etc.).
- **🔔 Solicitações Pendentes** — vive dentro de Administração, visível a qualquer aprovador cadastrado (não só ao dono).
- **ℹ️ Sobre o app** — esta mesma visão geral, com diagramas, dentro do próprio app.

### Controle de acesso e governança

- **Login com aprovação**: só o dono do app (`APP_OWNER_USERNAME`) entra direto. Qualquer outra pessoa precisa ser aprovada a cada nova sessão (a aprovação é consumida no login — não fica valendo pra sempre).
- **Sessão via cookie** (não mais via URL) — o token de sessão não aparece na barra de endereços.
- **PAT pessoal**: cada pessoa informa o próprio Personal Access Token do Azure DevOps no Passo 7/8 — nunca fica salvo em disco, só na memória da sessão.
- **Permissões granulares**: acesso à Integração com Azure DevOps (`azure_devops`) e ao Relatório de Testes (`execution_report`) são liberados individualmente pelo dono — quem não tem a permissão **nem vê o botão**.
- **Logs de auditoria**: todo evento relevante é registrado (quem, quando, onde, o quê), visível só para o dono, em Administração.

---

## Stack técnica

- **Frontend/Backend**: [Streamlit](https://streamlit.io/) (Python)
- **Orquestração de IA**: [n8n](https://n8n.io/) (self-hosted), com fallback entre até 5 provedores por workflow: Google Gemini, Groq (x2), OpenAI, Mistral
- **Sessão**: cookie assinado (HMAC), via `extra-streamlit-components`
- **PDF**: ReportLab
- **Extração de documentos**: PyMuPDF (`fitz`) para PDF, `python-docx` para DOCX
- **Azure DevOps**: REST API (`dev.azure.com`), autenticação via PAT

---

## Estrutura do projeto

```
app.py                              # ponto de entrada
.streamlit/secrets.toml             # nunca commitado
requirements.txt

qa_testgen/
├── config/
│   └── settings.py                 # AppConfiguration (lê st.secrets)
├── ui/
│   ├── application.py              # UserInterface — toda a lógica de tela
│   ├── auth.py                     # login, sessão (cookie), permissões, logout
│   └── dialogs.py                  # modais de confirmação
├── infrastructure/
│   ├── webhook_client.py           # chamadas aos webhooks de IA do n8n
│   ├── azure_devops_client.py      # cliente REST completo do Azure DevOps
│   ├── access_control_client.py    # cliente do webhook de controle de acesso/logs
│   ├── document_processor.py       # extração de texto + imagens dos documentos
│   ├── csv_formatter.py            # exportação CSV
│   └── pdf_report.py               # geração dos dois PDFs (Documentação QA e Relatório de Testes)
└── application/
    └── session.py                  # SessionState (defaults do st.session_state)
```

### Workflows do n8n (fora do repositório Python, vivem no n8n)

| Workflow | Função |
|---|---|
| `Doc_QA_Analysis` | Gera as perguntas de esclarecimento (Passo 2) |
| `Doc_QA_Matrix` | Gera a Matriz de Cobertura (Passo 3) |
| `Doc_QA_Generation` | Gera os Casos de Teste (Passo 4) |
| `Doc_QA_Plans` | Gera os Planos de Teste (Passo 5) |
| `Doc_QA_Matching` | Sugere vínculos Caso↔Work Item (Passo 7) |
| `Doc_QA_Access_Control` | Aprovações de login, permissões, logs de auditoria |
| `Doc_QA_Image_Interpretation` | Interpreta imagens extraídas dos documentos (Passo 1) |
| `Doc_QA_Execution_Report_Narrative` | Sugere Contexto/Escopo/Conclusão/Próximos Passos do Relatório de Testes |
| `Doc_QA_WIQL_Generation` | Traduz descrição em linguagem natural para WIQL |

---

## Configuração

### Dependências Python

```
streamlit
requests
reportlab
pymupdf
python-docx
pillow
bcrypt
extra-streamlit-components
```

Instalação:
```bash
pip install -r requirements.txt
```

### `secrets.toml`

Local: `.streamlit/secrets.toml`. Produção: Secrets do Streamlit Community Cloud (mesmo conteúdo).

```toml
# Webhooks do n8n
N8N_WEBHOOK_URL_ANALYSIS = "http://SEU-N8N/webhook/qa-testgen-analysis"
N8N_WEBHOOK_URL_MATRIX = "http://SEU-N8N/webhook/qa-testgen-matrix"
N8N_WEBHOOK_URL_GENERATION = "http://SEU-N8N/webhook/qa-testgen-generation"
N8N_WEBHOOK_URL_PLANS = "http://SEU-N8N/webhook/qa-testgen-plans"
N8N_WEBHOOK_URL_MATCHING = "http://SEU-N8N/webhook/qa-testgen-matching"
N8N_WEBHOOK_URL_ACCESS_CONTROL = "http://SEU-N8N/webhook/qa-testgen-access-control"
N8N_WEBHOOK_URL_IMAGE_INTERPRETATION = "http://SEU-N8N/webhook/qa-testgen-image-interpretation"
N8N_WEBHOOK_URL_EXECUTION_REPORT_NARRATIVE = "http://SEU-N8N/webhook/qa-testgen-execution-report-narrative"
N8N_WEBHOOK_URL_WIQL_GENERATION = "http://SEU-N8N/webhook/qa-testgen-wiql-generation"

# Autenticação dos webhooks (Header Auth no n8n) — use um valor longo e aleatório
N8N_API_KEY = "GERE_UM_VALOR_ALEATORIO_LONGO"

# Dono do app — esse usuário nunca precisa de aprovação de login
APP_OWNER_USERNAME = "admin"

# Valores padrão sugeridos no Passo 7 (opcional, mas AZURE_DEVOPS_ORG é
# importante como fallback quando o PAT é restrito a uma única organização)
AZURE_DEVOPS_ORG = "sua-organizacao"
AZURE_DEVOPS_PROJECT = ""

[credentials]
cookie_secret = "GERE_OUTRO_VALOR_ALEATORIO_LONGO"

[credentials.usernames]
admin = "$2b$12$...hash-bcrypt-aqui..."
outro_usuario = "$2b$12$...hash-bcrypt-aqui..."
```

> ⚠️ Não existe mais `AZURE_DEVOPS_PAT` — cada pessoa usa o próprio PAT, informado na tela (nunca salvo).

Gerando um hash bcrypt de senha:
```python
import bcrypt
print(bcrypt.hashpw(b"senha-da-pessoa", bcrypt.gensalt()).decode())
```

### Workflows do n8n

1. Importe cada workflow (`.json`) no n8n.
2. Vincule as credenciais de IA em cada node (Gemini, Groq, OpenAI, Mistral) — os nomes de credencial precisam já existir na sua instância.
3. Configure a credencial **Header Auth** (mesmo valor de `N8N_API_KEY`) no node Webhook de cada workflow.
4. Ative todos.

---

## Como rodar localmente

```bash
git clone <repo>
cd Documentacao_QA
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt --break-system-packages  # se necessário
# preencha .streamlit/secrets.toml
python -m streamlit run app.py
```

## Deploy

Streamlit Community Cloud com auto-deploy a partir do `main` — basta configurar os Secrets (mesmo conteúdo do `secrets.toml`) nas configurações do app.

---

## Conceitos importantes

### Ambiente (Homologação/Produção)

Escolhido obrigatoriamente no Passo 1 (sem pré-seleção). Define a etiqueta usada:
- Casos de Teste: `CT01 HML - <título>` / `CT01 PROD - <título>` (tela, CSV, PDF, e título real criado no Azure DevOps)
- Matriz: `MC-008 HML` / `MC-008 PROD`
- PDFs indicam o Ambiente no cabeçalho

### PAT pessoal

Informado no Passo 7 (e reaproveitado no Relatório de Testes / Gerar via Work Items / Criar Query, se já validado antes). Precisa dos escopos **Work Items (Read & Write)** e **Test Management (Read & Write)**. Nunca é salvo — só vive na sessão do navegador.

### Permissões granulares

Duas permissões, concedidas individualmente em Administração:
- `azure_devops` — Passo 7, Gerar via Work Items, Criar Query com IA
- `execution_report` — Relatório de Testes

Quem não tem a permissão não vê o botão correspondente na sidebar nem na barra de progresso.

### Status de QA via coluna do board

No Relatório de Testes, o Status de cada Caso não vem do "outcome" do Test Point (aba Execute), e sim da **coluna do quadro Kanban** do Work Item vinculado a ele (e dos filhos diretos desse Work Item):

| Status | Colunas do board |
|---|---|
| **Aprovado** | Pronto para UAT, Teste UAT, Aguardando CAB, Aguardando Subida em Produção, Testes em Produção, Finalizado |
| **Cancelado** | Cancelados |
| **Pendente** | Backlog, Em/Pronto para Refinamento de Negócios/Técnico, Em/Pronto para Validação de Produtos, Pronto para Dev, Em Desenvolvimento, Pronto/Em Code Review, Pronto para QA, Teste QA |
| **Fallback** | Se a coluna não bater com nenhuma lista acima, procura por "Aprovado/Reprovado em Homologação/Produção" na descrição do Work Item |

O **Status geral** do relatório (resumo do topo) é escolhido manualmente antes de gerar — não é mais calculado automaticamente.

---

## Limitações conhecidas

- **Matriz de Cobertura** nunca é enviada ao Azure DevOps — só existe no PDF quando gerada na mesma sessão (ou é reconstruída de forma independente, a partir dos Work Items vinculados, quando ausente).
- **Bloqueio de F5** no navegador é best-effort (via JS) — navegadores modernos podem ignorá-lo. A proteção garantida contra perda de dados do Relatório de Testes é a confirmação antes de navegar pra outro lugar.
- **Logs de auditoria** guardam só os últimos 500 eventos (armazenamento via n8n static data, sem banco de dados dedicado).
- Algumas áreas da API do Azure DevOps (evidências em anexos de Work Item, status por Board Column, criação de query WIQL) foram implementadas com base na documentação pública e ajustadas a partir de testes reais — se o formato da resposta variar entre organizações/processos, pode precisar de ajuste fino.

## Manutenção

- **PAT do time/organização** (se ainda usado em algum contexto administrativo): confira a validade periodicamente.
- **Modelos de IA**: os workflows do n8n fixam versões de modelo (`gemini-3.5-flash`, `gpt-5.4-mini` etc.) — provedores mudam/depreciam modelos com frequência, vale checar de tempos em tempos.
- **Credenciais do n8n**: se alguma credencial de IA expirar ou for revogada, o fallback para o próximo provedor da cadeia cobre a maioria dos casos, mas vale monitorar os logs de execução do n8n.