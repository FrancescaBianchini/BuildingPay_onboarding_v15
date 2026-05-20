# -*- coding: utf-8 -*-
{
    'name': 'BuildingPay Onboarding',
    'version': '17.0.2.0.0',
    'category': 'Custom',
    'summary': 'BuildingPay v30 - Gestione Amministratori Condomini e Pagamenti PagoPa',
    'description': """
BuildingPay - Modulo per la gestione degli amministratori di condomini,
portale web per registrazione e gestione contratti, importazione fatture
PagoPa e retrocessioni verso amministratori e referrer.
    """,
    'author': 'Progetto e Soluzioni',
    'website': 'https://www.progettiesoluzioni.it',
    'license': 'OPL-1',
    'depends': [
        'base',
        'base_setup',
        'website',
        'portal',
        'account',
        'product',
        'mail',
        'auth_signup',
        # l10n_it_edi NON è dichiarato come dipendenza perché è incompatibile con
        # altri moduli di fatturazione elettronica italiana (es. l10n_it_einvoice).
        # I campi fiscalcode, pec_mail, codice_destinatario ecc. sono forniti
        # da qualunque localizzazione italiana installata nell'ambiente.
        # Rileva e crea automaticamente res.bank dall'IBAN (usa schwifty internamente)
        'base_bank_from_iban',
        # CRM: gestione lead da richieste di informazioni portal
        'crm',
    ],
    'data': [
        # Security (load first)
        'security/buildingpay_security.xml',
        'security/ir.model.access.csv',
        # Regole record (dopo il CSV: i gruppi devono esistere già)
        'data/ir_rules_data.xml',
        # Data
        'data/mail_template_data.xml',
        'data/ir_cron_data.xml',
        # Views
        'views/buildingpay_config_views.xml',
        'views/res_partner_views.xml',
        'views/buildingpay_comune_istat_views.xml',
        'views/buildingpay_menus.xml',
        'views/crm_lead_views.xml',
        # Portal templates
        'templates/portal_home_inherit.xml',
        'templates/portal_registration.xml',
        'templates/portal_contratto.xml',
        'templates/portal_condomini.xml',
        'templates/portal_profilo.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'BuildingPay_onboarding_v15/static/src/js/iban_validator.js',
        ],
    },
    # schwifty è una dipendenza di base_bank_from_iban, non serve ripeterlo qui
    'external_dependencies': {},
    'installable': True,
    'application': True,
    'auto_install': False,
}
