# -*- coding: utf-8 -*-
from odoo import fields, models


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    referrer_id = fields.Many2one(
        comodel_name='res.partner',
        string='Referrer BuildingPay',
        domain="[('referrer_code', '!=', False)]",
        index=True,
        help='Referrer BuildingPay che ha indirizzato questo contatto.',
    )
