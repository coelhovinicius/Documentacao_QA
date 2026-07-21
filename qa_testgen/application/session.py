import streamlit as st

class SessionState:
    DEFAULTS = {
        'step': 1,
        'doc_text': '',
        'project_name': '',
        'uploaded_files': [],
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
        'ado_area_path': '',
        'ado_available_area_paths': [],
        'ado_area_paths_project': '',
        'ado_org_override': '',
        'ado_project_override': '',
        'ado_orgs_fetch_done': False,
        'ado_orgs_fetch_error': None,
        'ado_accessible_orgs': [],
        'ado_available_projects': [],
        'ado_projects_org': '',
        'ado_board_items': [],
        'ado_test_case_ids': {},
        'ado_wi_case_links': {},
        'ado_suggest_message': None,
        'ado_case_links': {},
        'ado_full_push_log': [],
        'show_ado_confirm_modal': False,
        'ado_confirm_modal_params': None,
        'ado_test_plan_name': '',
        'ado_tc_initial_state': 'Ready',
        'ado_plan_name_error': None,
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
