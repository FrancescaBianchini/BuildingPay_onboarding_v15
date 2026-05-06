# -*- coding: utf-8 -*-
import base64
import csv
import io
import re
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Mappa "nome colonna normalizzato" → "nome campo sul modello buildingpay.comune.istat"
# La normalizzazione rimuove cifre tra parentesi, spazi extra e converte in minuscolo.
_COLUMN_MAP = {
    'codice ripartizione geografica':           'cod_ripartizione',
    'denominazione ripartizione geografica':    'denominazione_ripartizione',
    'codice regione':                           'cod_regione',
    'denominazione regione':                    'denominazione_regione',
    'denominazione regione italiana e straniera': 'denominazione_regione',
    'codice citta metropolitana':               'cod_citta_metropolitana',
    'codice città metropolitana':               'cod_citta_metropolitana',
    'denominazione citta metropolitana':        'denominazione_citta_metropolitana',
    'denominazione città metropolitana':        'denominazione_citta_metropolitana',
    'denominazione citta metropolitana italiana e straniera': 'denominazione_citta_metropolitana',
    'denominazione città metropolitana italiana e straniera': 'denominazione_citta_metropolitana',
    'codice provincia storico':                 'cod_provincia_storico',
    'progressivo del comune':                   'progressivo_comune',
    'codice comune formato alfanumerico':        'cod_comune_alfanumerico',
    'denominazione in italiano':                'denominazione_it',
    'denominazione in tedesco':                 'denominazione_de',
    'denominazione altra lingua':               'denominazione_altra_lingua',
    'denominazione bilingue':                   'denominazione_altra_lingua',
    'codice comune formato numerico':            'cod_comune_numerico',
    'codice comune numerico con 110 province dal 2010': 'cod_comune_110',
    'codice comune numerico con 107 province dal 2006': 'cod_comune_107',
    'codice comune numerico con 103 province dal 1995': 'cod_comune_103',
    'codice catastale del comune':              'cod_catastale',
    'popolazione legale 2011':                  'popolazione_2011',
    'flag comune capoluogo di provincia citta metropolitana libero consorzio': 'flag_capoluogo',
    'flag comune capoluogo di provincia città metropolitana libero consorzio': 'flag_capoluogo',
    'sigla automobilistica':                    'sigla_auto',
    'codice nuts1 2021':                        'cod_nuts1',
    'codice nuts2 2021':                        'cod_nuts2',
    'codice nuts3 2021':                        'cod_nuts3',
    'codice nuts1 2010':                        'cod_nuts1',
    'codice nuts2 2010':                        'cod_nuts2',
    'codice nuts3 2010':                        'cod_nuts3',
    'codice nuts1':                             'cod_nuts1',
    'codice nuts2':                             'cod_nuts2',
    'codice nuts3':                             'cod_nuts3',
}


def _normalize_col(name):
    """Normalizza il nome della colonna: minuscolo, rimuove note a piè di pagina
    tra parentesi, apostrofi, trattini e spazi multipli."""
    n = name.lower().strip()
    n = re.sub(r'\s*\([^)]*\)', '', n)   # rimuove (1), (dal 2010), ecc.
    n = re.sub(r"[''']", '', n)          # rimuove apostrofi
    n = re.sub(r'[-–]', ' ', n)          # trattini → spazio
    n = re.sub(r'\s+', ' ', n).strip()
    return n


class WizardImportIstat(models.TransientModel):
    _name = 'buildingpay.wizard.import.istat'
    _description = 'Importazione codici ISTAT comuni'

    # ── Campi ────────────────────────────────────────────────────────────────
    csv_file = fields.Binary(
        string='File CSV ISTAT',
        required=True,
        help=(
            'Carica il file CSV scaricato da ISTAT:\n'
            'https://www.istat.it/storage/codici-unita-amministrative/Elenco-comuni-italiani.csv\n'
            'Formato atteso: UTF-8 (o UTF-8 con BOM), delimitatore punto e virgola (;).'
        ),
    )
    csv_filename = fields.Char(string='Nome file')
    cancella_esistenti = fields.Boolean(
        string='Cancella i dati esistenti prima di importare',
        default=True,
        help='Se attivo, svuota la tabella ISTAT prima di importare il nuovo file.',
    )

    # ── Risultati (sola lettura, popolati dopo l'importazione) ───────────────
    righe_importate = fields.Integer(string='Righe importate', readonly=True)
    righe_errore = fields.Integer(string='Righe con errori', readonly=True)
    state_aggiornate = fields.Integer(
        string='Partner aggiornati con codice ISTAT', readonly=True,
    )
    state = fields.Selection(
        [('draft', 'In attesa'), ('done', 'Completato')],
        default='draft',
        readonly=True,
    )
    result_message = fields.Text(string='Riepilogo', readonly=True)

    # ── Metodi ───────────────────────────────────────────────────────────────

    def _detect_delimiter(self, sample):
        """Rileva il delimitatore del CSV dal campione di testo."""
        counts = {';': sample.count(';'), ',': sample.count(','), '\t': sample.count('\t')}
        return max(counts, key=counts.get)

    def _parse_csv(self, csv_bytes):
        """Decodifica e parsa il CSV. Restituisce (header_map, rows_iter).
        header_map: dict { indice_colonna: nome_campo_odoo }
        rows_iter: iteratore sulle righe come liste di stringhe.
        """
        # prova prima UTF-8 BOM, poi UTF-8, poi latin-1
        for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'iso-8859-1'):
            try:
                text = csv_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise UserError(_('Impossibile decodificare il file CSV. Prova a salvarlo in UTF-8.'))

        delimiter = self._detect_delimiter(text[:2000])
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            raise UserError(_('Il file CSV è vuoto.'))

        # Intestazione
        raw_header = rows[0]
        header_map = {}
        for i, col in enumerate(raw_header):
            norm = _normalize_col(col)
            if norm in _COLUMN_MAP:
                field_name = _COLUMN_MAP[norm]
                header_map[i] = field_name

        if 'denominazione_it' not in header_map.values():
            raise UserError(_(
                'Colonna "Denominazione in italiano" non trovata nel CSV.\n'
                'Verificare che il file sia quello corretto di ISTAT.'
            ))

        return header_map, rows[1:]

    def action_import(self):
        """Importa il CSV ISTAT nella tabella buildingpay.comune.istat
        e aggiorna codice_istat su tutti i res.partner in base al campo Città."""
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_('Caricare il file CSV prima di procedere.'))

        csv_bytes = base64.b64decode(self.csv_file)
        header_map, data_rows = self._parse_csv(csv_bytes)

        IstatModel = self.env['buildingpay.comune.istat']

        # Cancella esistenti se richiesto
        if self.cancella_esistenti:
            IstatModel.search([]).unlink()

        # ── Importa righe ────────────────────────────────────────────────────
        records_vals = []
        righe_errore = 0

        for row in data_rows:
            if not any(c.strip() for c in row):
                continue  # riga vuota
            vals = {}
            for idx, field_name in header_map.items():
                if idx < len(row):
                    val = row[idx].strip()
                    if val:
                        vals[field_name] = val
            if not vals.get('denominazione_it'):
                righe_errore += 1
                continue
            records_vals.append(vals)

        # Crea in blocchi per efficienza
        CHUNK = 500
        created = 0
        for i in range(0, len(records_vals), CHUNK):
            chunk = records_vals[i:i + CHUNK]
            IstatModel.create(chunk)
            created += len(chunk)

        # ── Aggiorna codice_istat su tutti i res.partner ─────────────────────
        # Match: partner.city → denominazione_it (case-insensitive)
        # Usa cod_comune_alfanumerico (6 cifre con zero iniziale, es. "037006")
        partner_aggiornati = self._abbina_partner_codice_istat()

        # Messaggio riepilogo
        msg = _(
            'Importazione completata.\n'
            '• Righe importate: %d\n'
            '• Righe con errori/saltate: %d\n'
            '• Partner aggiornati con codice ISTAT: %d'
        ) % (created, righe_errore, partner_aggiornati)

        self.write({
            'righe_importate': created,
            'righe_errore': righe_errore,
            'state_aggiornate': partner_aggiornati,
            'state': 'done',
            'result_message': msg,
        })

        # Rimane nella stessa finestra per mostrare i risultati
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _abbina_partner_codice_istat(self):
        """Aggiorna codice_istat su tutti i res.partner in base a partner.city,
        cercando corrispondenza nella tabella buildingpay.comune.istat.
        Match: partner.city (case-insensitive) → denominazione_it.
        Imposta cod_comune_alfanumerico (es. "037006").
        Restituisce il numero di partner aggiornati."""
        IstatModel = self.env['buildingpay.comune.istat']

        istat_records = IstatModel.search([
            ('cod_comune_alfanumerico', '!=', False),
            ('denominazione_it', '!=', False),
        ])
        if not istat_records:
            return 0

        # Dizionario: nome comune normalizzato → codice alfanumerico
        istat_by_name = {
            rec.denominazione_it.strip().lower(): rec.cod_comune_alfanumerico
            for rec in istat_records
        }

        # Cerca tutti i partner con città valorizzata
        partners = self.env['res.partner'].search([('city', '!=', False)])
        aggiornati = 0
        for partner in partners:
            codice = istat_by_name.get(partner.city.strip().lower())
            if codice and partner.codice_istat != codice:
                # Scrittura diretta: 'city' non è in vals → non ri-esegue il lookup
                partner.sudo().write({'codice_istat': codice})
                aggiornati += 1

        return aggiornati

    def action_abbina_solo(self):
        """Aggiorna codice_istat su tutti i res.partner in base a partner.city
        (senza reimportare il CSV — utile se il file è già stato importato)."""
        self.ensure_one()
        IstatModel = self.env['buildingpay.comune.istat']

        if not IstatModel.search([('cod_comune_alfanumerico', '!=', False)], limit=1):
            raise UserError(_('Nessun dato ISTAT presente. Importare prima il CSV.'))

        partner_aggiornati = self._abbina_partner_codice_istat()

        msg = _('Abbinamento completato. Partner aggiornati con codice ISTAT: %d') % partner_aggiornati
        self.write({
            'state_aggiornate': partner_aggiornati,
            'state': 'done',
            'result_message': msg,
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
