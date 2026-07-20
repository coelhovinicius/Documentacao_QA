"""
Lista todos os campos do tipo de work item "Test Case" no projeto, via API
(só precisa de permissão de LEITURA — não precisa ser admin do processo).

Útil pra descobrir o "reference name" de campos customizados (como
"Pre condicoes") sem precisar abrir a tela de administração do processo.

Uso:
    python list_test_case_fields.py
"""

import base64
import getpass
import os

import requests


def main():
    org = os.getenv("AZURE_DEVOPS_ORG") or input("Organização do Azure DevOps: ").strip()
    project = os.getenv("AZURE_DEVOPS_PROJECT") or input("Nome do projeto: ").strip()
    pat = os.getenv("AZURE_DEVOPS_PAT") or getpass.getpass("PAT (não aparece ao digitar): ").strip()

    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}

    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/wit/workitemtypes/"
        f"Test%20Case/fields?api-version=7.1"
    )
    response = requests.get(url, headers=headers, timeout=30)

    if not response.ok:
        print(f"❌ Erro {response.status_code}: {response.text[:500]}")
        return

    fields = response.json().get("value", [])
    print(f"\nTotal de campos no tipo 'Test Case': {len(fields)}\n")

    print("=== Campos que parecem ser de Pré-condições ===")
    found = False
    for f in fields:
        name = f.get("name", "")
        if "condi" in name.lower() or "pre" in name.lower():
            found = True
            print(f"  Nome exibido: {name}")
            print(f"  Reference name: {f.get('referenceName')}")
            print()
    if not found:
        print("  (nenhum campo com 'condi' ou 'pre' no nome — veja a lista completa abaixo)\n")

    print("=== Lista completa de campos ===")
    for f in sorted(fields, key=lambda x: x.get("name", "")):
        print(f"  {f.get('name', ''):40s} -> {f.get('referenceName', '')}")


if __name__ == "__main__":
    main()
