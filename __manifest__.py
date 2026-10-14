{
    'name': 'students_management',
    'summary': 'Student Management System with voice-enabled native Odoo AI record operations',
    'description': """
Student Management System.

This build uses Odoo 19 Enterprise's built-in Ask AI. Ask AI can answer small
Odoo doubts and use permission-aware tools to inspect, search, create, and edit
supported Student Management and common Odoo records. Official Helpdesk ticket
tools are kept as dedicated operations. It does not use helpdesk_mgmt or a
custom chatbot. A separate Groq backup panel is also included so users can
continue asking Odoo questions and running permission-aware tools when the
primary Odoo AI provider is unavailable. Both the native Ask AI microphone and
the G backup microphone use Groq whisper-large-v3 and only fill their chat box;
voice transcription never auto-sends a message.
    """,
    'author': 'My Company',
    'category': 'Uncategorized',
    'version': '19.0.0.23.0',
    'depends': [
        'base',
        'web',
        'mail',
        'sale',
        'helpdesk',
        'ai_app',
        'ai_server_actions',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'reports/student_report.xml',
        'data/sequence.xml',
        'views/student.xml',
        'views/sales_inherit_model.xml',
        'views/department.xml',
        'views/fee_structure.xml',
        'views/teacher.xml',
        'views/teacher_leave.xml',
        'views/semester.xml',
        'views/subject.xml',
        'wizard/wizard.xml',
        'views/search.xml',
        'data/teacher_leave_mail_template.xml',
        'data/teacher_leave_cron.xml',
        'views/views_menu.xml',
        # Execute on install AND module upgrade. This is deliberately data-driven
        # rather than post_init_hook-only because this module is already installed
        # in the development database.
        'data/helpdesk_native_ai_setup.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'students_management/static/src/js/accurate_voice_input.js',
            'students_management/static/src/scss/accurate_voice_input.scss',
            'students_management/static/src/js/groq_fallback_widget.js',
            'students_management/static/src/scss/groq_fallback_widget.scss',
        ],
    },
    'external_dependencies': {
        # Odoo checks this against the installed *distribution* name via
        # importlib.metadata, not the Python import name -- the package is
        # imported as `whisper` but distributed on PyPI as `openai-whisper`,
        # so it must be listed here as 'openai-whisper' or this check always
        # fails even when `import whisper` works fine.
        'python': ['openai-whisper'],
        'bin': ['ffmpeg'],
    },
    'demo': [],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
