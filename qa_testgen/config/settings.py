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
        self.api_key = self._get('N8N_API_KEY', '')

        self.azure_devops_org = self._get('AZURE_DEVOPS_ORG', '')
        self.azure_devops_project = self._get('AZURE_DEVOPS_PROJECT', '')
        self.azure_devops_pat = self._get('AZURE_DEVOPS_PAT', '')

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
