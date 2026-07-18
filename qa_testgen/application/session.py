import streamlit as st

class SessionState:
    DEFAULTS = {
        'step': 1,
        'doc_text': '',
        'project_name': '',
        'uploaded_file': None,
        'questions': [],
        'user_answers': {},
        'step_2_answers': {},
        'matriz': [],
        'test_cases': [],
        'test_plans': [],
        'csv_cases': '',
        'csv_plans': '',
        'is_processing': False,
        'current_action': None,
        'processing_interrupted': False,
        'max_step': 1,
        'completed_steps': [],
        'adding_matriz_row': False,
        'adding_test_case': False,
        'adding_test_plan': False,
        'active_matriz_row': None,
        'active_test_case_row': None,
        'active_test_plan_row': None,
        'ado_push_scope': 'Nenhum',
        'ado_test_case_ids': {},
        'ado_plan_ids': {},
        'ado_push_log': [],
    }

    def __init__(self):
        self._state = st.session_state
        self.initialize()

    def initialize(self):
        for key, value in self.DEFAULTS.items():
            if key not in self._state:
                self._state[key] = value

    def clear(self):
        self._state.clear()

    def get(self, key, default=None):
        return self._state.get(key, default)

    def set(self, key, value):
        self._state[key] = value

    def __getitem__(self, key):
        return self._state[key]

    def __setitem__(self, key, value):
        self._state[key] = value

    def __contains__(self, key):
        return key in self._state

    def keys(self):
        return self._state.keys()

    def delete(self, key):
        if key in self._state:
            del self._state[key]
