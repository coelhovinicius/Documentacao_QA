"""
Teste isolado de conexão com o Azure DevOps — roda fora do Streamlit.

Uso:
    python test_azure_devops_connection.py

Vai pedir org/projeto/PAT (ou usar variáveis de ambiente, se preferir) e
tentar, passo a passo:
    1) Criar 1 Test Case simples
    2) Criar 1 Test Plan
    3) Criar 1 Test Suite dentro do plano
    4) Vincular o Test Case criado à Suite

Se qualquer etapa falhar, o erro aparece detalhado no terminal — não precisa
abrir o navegador nem navegar pelo app pra descobrir o que deu errado.
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from azure_devops_client import AzureDevOpsClient, AzureDevOpsError


def get_config():
    org = os.getenv("AZURE_DEVOPS_ORG") or input("Organização do Azure DevOps: ").strip()
    project = os.getenv("AZURE_DEVOPS_PROJECT") or input("Nome do projeto: ").strip()
    pat = os.getenv("AZURE_DEVOPS_PAT") or getpass.getpass("PAT (não aparece ao digitar): ").strip()
    return org, project, pat


def main():
    org, project, pat = get_config()
    client = AzureDevOpsClient(org, project, pat)

    if not client.is_configured():
        print("❌ Organização, projeto ou PAT vazios. Confira os valores e tente de novo.")
        return

    print(f"\n🔗 Conectando em: https://dev.azure.com/{org}/{project}\n")

    # 1) Criar Test Case
    print("1) Criando um Test Case de teste...")
    try:
        result = client.create_test_case(
            titulo="[TESTE DE CONEXÃO] Caso de exemplo",
            pre_condicoes="Usuário autenticado no sistema.",
            passos=[
                {"numero": 1, "acao": "Abrir a tela X", "resultado_esperado": "A tela X é exibida"},
                {"numero": 2, "acao": "Clicar em Salvar", "resultado_esperado": "Mensagem de sucesso aparece"},
            ],
        )
        case_id = result["id"]
        print(f"   ✅ Test Case criado com sucesso! ID: {case_id}")
        print(f"   🔗 {client.work_item_url(case_id)}")
    except AzureDevOpsError as e:
        print(f"   ❌ Falhou: {e}")
        return
    except Exception as e:
        print(f"   ❌ Erro inesperado: {e}")
        return

    # 2) Criar Test Plan
    print("\n2) Criando um Test Plan de teste...")
    try:
        plan = client.create_test_plan(
            nome="[TESTE DE CONEXÃO] Plano de exemplo",
            descricao="Plano criado pelo script de teste de conexão.",
        )
        plan_id = plan["id"]
        root_suite_id = plan["root_suite_id"]
        print(f"   ✅ Test Plan criado com sucesso! ID: {plan_id} (root suite: {root_suite_id})")
        print(f"   🔗 {client.test_plan_url(plan_id)}")
    except AzureDevOpsError as e:
        print(f"   ❌ Falhou: {e}")
        return
    except Exception as e:
        print(f"   ❌ Erro inesperado: {e}")
        return

    if not root_suite_id:
        print("   ⚠️ Não recebi o ID da suite raiz do plano — não dá pra continuar os próximos passos.")
        return

    # 3) Criar Test Suite
    print("\n3) Criando uma Test Suite de teste dentro do plano...")
    try:
        suite_id = client.create_test_suite(plan_id, root_suite_id, "[TESTE DE CONEXÃO] Suite de exemplo")
        print(f"   ✅ Suite criada com sucesso! ID: {suite_id}")
    except AzureDevOpsError as e:
        print(f"   ❌ Falhou: {e}")
        return
    except Exception as e:
        print(f"   ❌ Erro inesperado: {e}")
        return

    # 4) Vincular o Test Case à Suite
    print("\n4) Vinculando o Test Case criado à Suite...")
    try:
        client.add_cases_to_suite(plan_id, suite_id, [case_id])
        print("   ✅ Vínculo feito com sucesso!")
    except AzureDevOpsError as e:
        print(f"   ❌ Falhou: {e}")
        return
    except Exception as e:
        print(f"   ❌ Erro inesperado: {e}")
        return

    print("\n🎉 Tudo funcionou! Pode conferir no Azure DevOps:")
    print(f"   Test Case: {client.work_item_url(case_id)}")
    print(f"   Test Plan: {client.test_plan_url(plan_id)}")
    print("\nDica: apague esses itens de teste no Azure DevOps depois de conferir, "
          "pra não poluir o projeto sandbox.")


if __name__ == "__main__":
    main()
