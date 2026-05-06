# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class BuildingPayComuneIstat(models.Model):
    """
    Tabella codici ISTAT per comune.

    Rispecchia tutte le colonne del file ufficiale ISTAT:
    https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.csv
    Delimitatore: punto e virgola (;)
    Encoding: UTF-8 (con o senza BOM)
    """
    _name = 'buildingpay.comune.istat'
    _description = 'Codici ISTAT per comune (ISTAT)'
    _order = 'denominazione_it'
    _rec_name = 'denominazione_it'

    # ── Ripartizione geografica ──────────────────────────────────────────────
    cod_ripartizione = fields.Char(
        string='Codice Ripartizione Geografica',
    )
    denominazione_ripartizione = fields.Char(
        string='Denominazione Ripartizione Geografica',
    )

    # ── Regione ─────────────────────────────────────────────────────────────
    cod_regione = fields.Char(
        string='Codice Regione',
        index=True,
    )
    denominazione_regione = fields.Char(
        string='Denominazione Regione',
    )

    # ── Città metropolitana ──────────────────────────────────────────────────
    cod_citta_metropolitana = fields.Char(
        string='Codice Città Metropolitana',
    )
    denominazione_citta_metropolitana = fields.Char(
        string='Denominazione Città Metropolitana',
    )

    # ── Provincia ────────────────────────────────────────────────────────────
    cod_provincia_storico = fields.Char(
        string='Codice Provincia (Storico)',
    )
    progressivo_comune = fields.Char(
        string='Progressivo del Comune',
    )

    # ── Codici comune ────────────────────────────────────────────────────────
    cod_comune_alfanumerico = fields.Char(
        string='Codice Comune formato alfanumerico',
        index=True,
    )
    cod_comune_numerico = fields.Char(
        string='Codice Comune formato numerico',
        index=True,
    )
    cod_comune_110 = fields.Char(
        string='Codice Comune numerico con 110 province (dal 2010)',
    )
    cod_comune_107 = fields.Char(
        string='Codice Comune numerico con 107 province (dal 2006)',
    )
    cod_comune_103 = fields.Char(
        string='Codice Comune numerico con 103 province (dal 1995)',
    )

    # ── Denominazioni ────────────────────────────────────────────────────────
    denominazione_it = fields.Char(
        string='Denominazione in italiano',
        index=True,
        required=True,
    )
    denominazione_de = fields.Char(
        string='Denominazione in tedesco',
    )
    denominazione_altra_lingua = fields.Char(
        string='Denominazione altra lingua',
    )

    # ── Altri dati ───────────────────────────────────────────────────────────
    cod_catastale = fields.Char(
        string='Codice Catastale del comune',
        index=True,
    )
    popolazione_2011 = fields.Char(
        string='Popolazione Legale 2011',
    )
    flag_capoluogo = fields.Char(
        string='Flag Comune capoluogo di Provincia/Città Metropolitana',
    )
    sigla_auto = fields.Char(
        string='Sigla Automobilistica',
    )

    # ── Codici NUTS ──────────────────────────────────────────────────────────
    cod_nuts1 = fields.Char(
        string='Codice NUTS1',
    )
    cod_nuts2 = fields.Char(
        string='Codice NUTS2',
    )
    cod_nuts3 = fields.Char(
        string='Codice NUTS3',
    )

    # ── Collegamento a res.country.state (provincia) ────────────────────────
    state_id = fields.Many2one(
        comodel_name='res.country.state',
        string='Provincia (res.country.state)',
        ondelete='set null',
        index=True,
        help='Collegamento alla provincia corrispondente.',
    )
