import streamlit as st

class SessionState:
    DEFAULTS = {
        'step': 1,
        'doc_text': '',
        'project_name': '',
        'ambiente_testes': '',
        'uploaded_files': [],
        'questions': [],
        'user_answers': {},
        'step_2_answers': {},
        'matriz': [],
        'test_cases': [],
        'test_plans': [],
        'csv_cases': '',
        'csv_plans': '',
        'pdf_report_bytes': None,
        'pdf_report_fingerprint': None,
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
        'report_available_plans': [],
        'report_wi_board_items': [],
        'report_contexto': '',
        'report_escopo': '',
        'report_conclusao': '',
        'report_proximos': '',
        'report_pdf_bytes': None,
        'report_warnings': [],
        '_perm_cache_azure_devops': None,
        '_perm_cache_execution_report': None,
        'ado_user_pat': '',
        'ado_last_validated_pat': None,
        'ado_pat_validated': None,
        'ado_area_path_choice': '---',
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
        'ado_wi_matching_selected_ids': None,
        'ado_test_case_ids': {},
        'ado_excluded_case_titles': [],
        'ado_wi_case_links': {},
        'ado_wi_prelinked_marker': None,
        'ado_wi_prelink_diag': None,
        'ado_duplicate_case_titles': [],
        'ado_suggest_message': None,
        'ado_case_links': {},
        'ado_full_push_log': [],
        'show_ado_confirm_modal': False,
        'show_static_confirm_modal': False,
        'ado_static_confirm_modal_params': None,
        'show_recon_confirm_modal': False,
        'ado_recon_confirm_modal_params': None,
        'show_about_page': False,
        'show_admin_page': False,
        'show_execution_report_page': False,
        'show_new_report_modal': False,
        'show_leave_report_modal': False,
        '_pending_navigation_after_report': None,
        '_f5_block_injected': False,
        'wigen_board_items': [],
        'wigen_selected_ids': [],
        'show_wiql_generation_page': False,
        'wiql_descricao': '',
        'wiql_generated': None,
        'wiql_preview_result': None,
        'ado_confirm_modal_params': None,
        'ado_test_plan_name': '',
        'ado_existing_plans_in_path': [],
        'ado_static_existing_plans': [],
        'ado_static_existing_plans_path': '',
        'ado_static_plan_name': '',
        'ado_static_push_log': [],
        'ado_recon_available_plans': [],
        'ado_recon_old_plan_id': None,
        'ado_recon_old_cases': [],
        'ado_recon_board_items': [],
        'ado_recon_wi_case_links': {},
        'ado_recon_push_log': [],
        'step1_board_items': [],
        'step1_doc_work_item_map': {},
        'tipo_documento': '',
        'ado_existing_plans_area_path': '',
        'ado_existing_plan_id_chosen': None,
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
