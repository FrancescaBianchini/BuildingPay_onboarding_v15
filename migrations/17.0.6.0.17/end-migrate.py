# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """I condomini diventano entità commerciali (is_company): da qui in poi fatture,
    crediti e mandati SDD sono intestati al condominio e non all'amministratore.

    Le registrazioni già confermate non vengono toccate: restano coerenti
    sull'amministratore. Su fatture e ordini in bozza Odoo non ricalcola da solo
    partner commerciale, partner delle righe contabili e mandato, perché dipendono
    solo dal partner del documento: li ricalcoliamo qui.

    Script "end" e non "post": i moduli OCA dei mandati non dipendono da questo
    modulo e nella fase post i loro campi non sono ancora nel registro.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    condomini = env['res.partner'].with_context(active_test=False).search([
        ('type', '=', 'condominio'),
        ('is_company', '=', False),
    ])
    if not condomini:
        return
    condomini.write({'is_company': True})
    _logger.info('BuildingPay: %d condomini impostati come entità commerciale.', len(condomini))

    Move = env['account.move']
    draft_moves = Move.search([('state', '=', 'draft'), ('partner_id', 'in', condomini.ids)])
    env.add_to_compute(Move._fields['commercial_partner_id'], draft_moves)
    env.add_to_compute(env['account.move.line']._fields['partner_id'], draft_moves.line_ids)
    if 'mandate_id' in Move._fields:
        env.add_to_compute(Move._fields['mandate_id'], draft_moves.filtered(lambda m: not m.mandate_id))
    _logger.info('BuildingPay: ricalcolate %d fatture in bozza dei condomini.', len(draft_moves))

    Order = env['sale.order']
    if 'mandate_id' in Order._fields:
        draft_orders = Order.search([
            ('state', 'in', ('draft', 'sent')),
            ('partner_invoice_id', 'in', condomini.ids),
            ('mandate_id', '=', False),
        ])
        env.add_to_compute(Order._fields['mandate_id'], draft_orders)
        _logger.info('BuildingPay: ricalcolato il mandato su %d ordini in bozza.', len(draft_orders))
    env.flush_all()
