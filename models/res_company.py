# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    """
    Estende res.company con i campi BuildingPay a livello di azienda.

    buildingpay_firma_link: URL del link Aruba per la firma elettronica,
    mostrato nella mail di benvenuto all'amministratore.
    Configurabile da Impostazioni → Aziende → [azienda] oppure da
    Impostazioni → (sezione BuildingPay se presente).
    """
    _inherit = 'res.company'

    buildingpay_firma_link = fields.Char(
        string='Link firma elettronica Aruba',
        help=(
            'URL del portale Aruba per la firma elettronica del contratto. '
            'Se valorizzato, viene incluso nella mail di benvenuto inviata '
            'al nuovo amministratore al momento della registrazione.'
        ),
    )
