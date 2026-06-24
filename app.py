import streamlit as st
import requests
import json
import os
from PyPDF2 import PdfReader
from docx import Document

class WebhookClient:
    def __init__(self, config):
        self.config = config
        self.headers = {"x-api-key": st.secrets["N8N_API_KEY"]}

    def trigger_generation(self, document_text: str, user_answers: dict, project_name: str) -> dict:
        payload = {
            "document_text": document_text,
            "respostas_duvidas": json.dumps(user_answers, ensure_ascii=False),
            "nome_projeto": project_name
        }
        response = requests.post(self.config.webhook_generation, json=payload, headers=self.headers, timeout=300)
        response.raise_for_status()
        
        data = response.json()
        # DIAGNÓSTICO: Se o CSV vem em branco, o erro está no print abaixo.
        print(f"DEBUG N8N RESPONSE: {json.dumps(data, indent=2)}") 
        
        # O n8n deve entregar um objeto com a chave 'casos_de_teste'.
        # Se vier uma lista direto (por causa do Merge), tratamos aqui:
        test_cases = data if isinstance(data, list) else data.get("casos_de_teste", [])
        return {"casos_de_teste": test_cases}

# --- AzureCsvFormatter: Garante formatação à prova de falhas ---
class AzureCsvFormatter:
    @staticmethod
    def generate_csv_content(test_cases: list, project_name: str) -> str:
        # Adiciona verificação de existência de dados
        if not test_cases: return "ID;Title;..." 
        
        csv_header = "ID;Work Item Type;Title;Test Step;Pre condicoes;Step Action;Step Expected;Automation Status;Area Path;Assigned To;State"
        csv_lines = [csv_header]
        for tc in test_cases:
            # Uso de .get() com defaults evita KeyError
            titulo = str(tc.get('titulo', 'N/A')).replace(';', ',')
            pre = str(tc.get('pre_condicoes', 'N/A')).replace(';', ',')
            csv_lines.append(f";Test Case;{titulo};;{pre};;;Not Automated;{project_name};;Design")
            for step in tc.get('passos', []):
                acao = str(step.get('acao', 'N/A')).replace(';', ',')
                esp = str(step.get('resultado_esperado', 'N/A')).replace(';', ',')
                csv_lines.append(f";;;{step.get('numero', '')};;{acao};{esp};;;;")
        return "\n".join(csv_lines)