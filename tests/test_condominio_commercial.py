# -*- coding: utf-8 -*-
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged('post_install', '-at_install')
class TestCondominioCommercialPartner(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        Partner = cls.env['res.partner']
        cls.amministratore = Partner.create({
            'name': 'Studio Rossi', 'is_company': True, 'vat': 'IT00743110157'})
        cls.condominio = Partner.create({
            'name': 'Condominio Via Roma 1',
            'type': 'condominio',
            'is_company': False,
            'parent_id': cls.amministratore.id,
            'vat': 'IT01234560017',
        })

    def test_condominio_is_commercial_entity(self):
        self.assertTrue(self.condominio.is_company)
        self.assertEqual(self.condominio.commercial_partner_id, self.condominio)
        self.assertEqual(self.condominio.parent_id, self.amministratore)

    def test_parent_vat_not_synced(self):
        self.amministratore.vat = 'IT02345670026'
        self.assertEqual(self.condominio.vat, 'IT01234560017')

    def test_cannot_turn_into_person(self):
        with self.assertRaises(UserError):
            self.condominio.is_company = False
        self.condominio.write({'type': 'other', 'is_company': False})
        self.assertEqual(self.condominio.commercial_partner_id, self.amministratore)

    def test_type_change_makes_company(self):
        contact = self.env['res.partner'].create({
            'name': 'Condominio Via Verdi 2', 'parent_id': self.amministratore.id})
        self.assertEqual(contact.commercial_partner_id, self.amministratore)
        contact.type = 'condominio'
        self.assertEqual(contact.commercial_partner_id, contact)

    def test_invoice_receivable_on_condominio(self):
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.condominio.id,
            'invoice_line_ids': [Command.create({'name': 'Servizio PagoPA', 'price_unit': 100.0})],
        })
        invoice.action_post()
        receivable = invoice.line_ids.filtered(lambda l: l.display_type == 'payment_term')
        self.assertEqual(receivable.partner_id, self.condominio)
