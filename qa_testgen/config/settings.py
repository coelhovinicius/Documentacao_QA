import os
from dotenv import load_dotenv
import streamlit as st

load_dotenv()

class AppConfiguration:
    def __init__(self):
        self.webhook_analysis = self._get(
            'N8N_WEBHOOK_URL_ANALYSIS',
            'http://localhost:5678/webhook/qa-testgen-analysis'
        )
        self.webhook_matrix = self._get(
            'N8N_WEBHOOK_URL_MATRIX',
            'http://localhost:5678/webhook/qa-testgen-matrix'
        )
        self.webhook_generation = self._get(
            'N8N_WEBHOOK_URL_GENERATION',
            'http://localhost:5678/webhook/qa-testgen-generation'
        )
        self.webhook_plans = self._get(
            'N8N_WEBHOOK_URL_PLANS',
            'http://localhost:5678/webhook/qa-testgen-plans'
        )
        self.webhook_matching = self._get(
            'N8N_WEBHOOK_URL_MATCHING',
            'http://localhost:5678/webhook/qa-testgen-matching'
        )
        self.webhook_access_control = self._get(
            'N8N_WEBHOOK_URL_ACCESS_CONTROL',
            'http://localhost:5678/webhook/qa-testgen-access-control'
        )
        self.webhook_image_interpretation = self._get(
            'N8N_WEBHOOK_URL_IMAGE_INTERPRETATION',
            'http://localhost:5678/webhook/qa-testgen-image-interpretation'
        )
        self.webhook_execution_report_narrative = self._get(
            'N8N_WEBHOOK_URL_EXECUTION_REPORT_NARRATIVE',
            'http://localhost:5678/webhook/qa-testgen-execution-report-narrative'
        )
        self.webhook_wiql_generation = self._get(
            'N8N_WEBHOOK_URL_WIQL_GENERATION',
            'http://localhost:5678/webhook/qa-testgen-wiql-generation'
        )
        self.webhook_manual_generation = self._get(
            'N8N_WEBHOOK_URL_MANUAL_GENERATION',
            'http://localhost:5678/webhook/qa-testgen-manual-generation'
        )
        self.webhook_duplicate_comparison = self._get(
            'N8N_WEBHOOK_URL_DUPLICATE_COMPARISON',
            'http://localhost:5678/webhook/qa-testgen-duplicate-comparison'
        )
        self.api_key = self._get('N8N_API_KEY', '')

        self.azure_devops_org = self._get('AZURE_DEVOPS_ORG', '')
        self.azure_devops_project = self._get('AZURE_DEVOPS_PROJECT', '')
        self.azure_devops_pat = self._get('AZURE_DEVOPS_PAT', '')

        # Dono do app — esse usuário nunca precisa de aprovação pra logar, e é
        # sempre um aprovador implícito (não precisa ser cadastrado à parte).
        self.owner_username = self._get('APP_OWNER_USERNAME', 'admin')

    def _get(self, key: str, default: str) -> str:
        value = os.getenv(key)
        if value:
            return value
        try:
            if key in st.secrets:
                return st.secrets[key]
        except Exception:
            pass
        return default
