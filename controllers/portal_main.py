# -*- coding: utf-8 -*-
import base64
import logging
from datetime import date
from io import BytesIO
from odoo import http, _
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager
from odoo.exceptions import AccessError, UserError
try:
    from odoo.addons.base_iban.models.res_partner_bank import validate_iban
except ImportError:
    validate_iban = None

_logger = logging.getLogger(__name__)


class BuildingPayPortal(CustomerPortal):
    """
    Controller portale BuildingPay.
    Aggiunge sezioni:
    - Contratti: due documenti distinti per l'amministratore
        1. Accordo Retrocessioni Amministratore ED (download pre-compilato + upload firmato)
        2. Accordo Condomini Aggregati ED (download con Allegato A + upload firmato)
    - Condomini: CRUD indirizzi di tipo 'condominio'
    """

    # -------------------------------------------------------
    # Home portale: aggiunge le sezioni BuildingPay
    # -------------------------------------------------------
    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)

        # ---------------------------------------------------------------
        # is_amministratore viene aggiunto SOLO sul rendering iniziale
        # della pagina (counters == []), MAI sulle chiamate AJAX di
        # aggiornamento contatori (counters == ['invoice_count', ...]).
        #
        # Motivo: il JS di Odoo 17 EE, alla risposta di /my/home/counts,
        # itera su TUTTE le chiavi del dict restituito e chiama:
        #   document.getElementById(key).textContent = value
        # Se una chiave (es. 'is_amministratore') non ha un corrispondente
        # <span id="is_amministratore"> nel DOM, getElementById restituisce
        # null → TypeError "Cannot set properties of null (setting 'textContent')".
        #
        # Sul rendering iniziale (counters=[]) il valore serve al template
        # per il t-if="is_amministratore". Sulle chiamate AJAX il template
        # NON viene ri-renderizzato, quindi il valore non serve.
        # ---------------------------------------------------------------
        if not counters:
            partner = request.env.user.partner_id.sudo()
            values['is_amministratore'] = partner.is_amministratore

        return values

    # -------------------------------------------------------
    # SEZIONE: Contratti (pagina principale)
    # -------------------------------------------------------
    # -------------------------------------------------------
    # IBAN validation + bank lookup endpoint
    # Chiamato via fetch() dal JS delle form signup e condomini.
    # Auth 'public': usato anche prima della registrazione (utente non autenticato).
    # Delega a res.partner.bank._get_bank_from_iban() del modulo
    # base_bank_from_iban, che usa schwifty con l'API corretta:
    #   iban.bank → dict {name, bic, bank_code}  (NON iban.bic.bank_name)
    # -------------------------------------------------------
    @http.route('/buildingpay/validate_iban', type='json', auth='public', website=True,
                csrf=False)
    def validate_iban(self, iban='', **kw):
        """
        Valida un IBAN e rileva la banca tramite base_bank_from_iban.

        Restituisce:
          { valid: bool, bank_id: int|None, bank_name: str, bic: str, error: str }

        La logica di validazione e lookup è interamente delegata al metodo
        res.partner.bank._get_bank_from_iban() di base_bank_from_iban, che:
          - lancia InvalidStructure / InvalidChecksumDigits se IBAN non valido
          - usa iban.bank (dict) per ricavare nome, BIC e codice banca
          - cerca o crea il record res.bank corrispondente
        """
        iban_clean = (iban or '').replace(' ', '').upper().strip()
        if not iban_clean:
            return {'valid': False, 'error': 'Inserire un IBAN',
                    'bank_id': None, 'bank_name': '', 'bic': ''}

        PartnerBank = request.env['res.partner.bank'].sudo()

        try:
            # _get_bank_from_iban lancia schwifty.exceptions.InvalidStructure /
            # InvalidChecksumDigits se il formato o il checksum è errato,
            # altrimenti restituisce un recordset res.bank (vuoto se banca
            # non trovata nel registro schwifty).
            bank = PartnerBank._get_bank_from_iban(iban_clean)
        except Exception as e:
            # IBAN non valido (struttura o checksum)
            _logger.info('BuildingPay validate_iban: IBAN non valido "%s": %s',
                         iban_clean, e)
            return {'valid': False, 'error': 'IBAN non valido',
                    'bank_id': None, 'bank_name': '', 'bic': ''}

        if bank:
            return {
                'valid':     True,
                'bank_id':   bank.id,
                'bank_name': bank.name or '',
                'bic':       bank.bic  or '',
            }
        else:
            # IBAN valido ma banca non presente nel registro schwifty
            return {
                'valid':     True,
                'bank_id':   None,
                'bank_name': '',
                'bic':       '',
            }

    @http.route('/my/contratti', type='http', auth='user', website=True)
    def portal_contratti(self, **kw):
        """Pagina Contratti nel portale: mostra i due documenti disponibili."""
        partner = request.env.user.partner_id
        config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()

        # Conta condomini attivi per mostrare/nascondere l'Accordo Condomini
        condomini_count = request.env['res.partner'].sudo().search_count([
            ('parent_id', '=', partner.id),
            ('type', '=', 'condominio'),
            ('active', '=', True),
        ])

        # Referrer dell'amministratore: i campi contratto retrocessione
        # (contratto_retrocessione_standard, contratto_retrocessione_custom_file, ecc.)
        # vengono letti da questo record, non dal partner stesso.
        referrer = partner.sudo().referrer_id or False

        values = {
            'partner': partner,
            'referrer': referrer,
            'config': config,
            'condomini_count': condomini_count,
            'page_name': 'contratti',
            'success': kw.get('success'),
            'error': kw.get('error'),
        }
        return request.render('BuildingPay_onboarding_v15.portal_contratti', values)

    # -------------------------------------------------------
    # CONTRATTO 1: Accordo Retrocessioni Amministratore ED
    # Download con placeholder: NOME AMMINISTRATORE, CODICE FISCALE, IBAN, NOME BANCA, DATA
    # -------------------------------------------------------
    @http.route('/my/contratti/retrocessioni/download', type='http', auth='user', website=True)
    def portal_retrocessioni_download(self, **kw):
        """
        Download del template 'Accordo Retrocessioni Amministratore ED'.
        Placeholder supportati nel template .docx:
        - [NOME AMMINISTRATORE] → partner.name
        - [CF]                  → partner.fiscalcode
        - [IBAN]                → IBAN completo (acc_number senza spazi)
        - [Paese]               → codice paese IBAN (es. IT)
        - [EUR]                 → check digits IBAN (es. 60)
        - [CIN]                 → carattere di controllo nazionale (es. X)
        - [ABI]                 → codice ABI 5 cifre
        - [CAB]                 → codice CAB 5 cifre
        - [N.CONTO]             → numero conto 12 caratteri
        - [DATA]                → data odierna DD/MM/YYYY
        Il file viene convertito in PDF prima del download.
        """
        partner = request.env.user.partner_id.sudo()
        config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()

        if not config or not config.accordo_retrocessioni_template:
            return request.redirect('/my/contratti?error=no_template_retrocessioni')

        try:
            nome_amministratore = partner.name or ''
            codice_fiscale = partner.fiscalcode or ''

            bank = request.env['res.partner.bank'].sudo().search([
                ('partner_id', '=', partner.id),
            ], limit=1)

            iban_raw = (bank.acc_number if bank else '') or ''
            iban = iban_raw.replace(' ', '').upper()

            # Scomposizione IBAN italiano: IT(2) + EUR/check(2) + CIN(1) + ABI(5) + CAB(5) + CC(12)
            # Parsing leniente: funziona anche se l'IBAN non è esattamente 27 char
            if len(iban) >= 5 and iban[:2].isalpha():
                paese   = iban[0:2]
                eur     = iban[2:4]
                cin     = iban[4:5]
                abi     = iban[5:10]  if len(iban) > 5  else ''
                cab     = iban[10:15] if len(iban) > 10 else ''
                n_conto = iban[15:]   if len(iban) > 15 else ''
            else:
                paese = eur = cin = abi = cab = n_conto = ''

            oggi = date.today().strftime('%d/%m/%Y')

            istituto = (bank.bank_id.name if bank and bank.bank_id else '') or ''
            intestatario = (bank.acc_holder_name or nome_amministratore) if bank else nome_amministratore

            replacements = {
                '[NOME AMMINISTRATORE]':  nome_amministratore,
                '[Intestatario]':         intestatario,
                '[INTESTATARIO]':         intestatario,
                '[CF]':                   codice_fiscale,
                '[CODICE FISCALE]':       codice_fiscale,
                '[________]':             codice_fiscale,
                '[IBAN]':                 iban,
                '[Paese]':                paese,
                '[PAESE]':                paese,
                '[EUR]':                  eur,
                '[CIN]':                  cin,
                '[ABI]':                  abi,
                '[CAB]':                  cab,
                '[N.CONTO]':              n_conto,
                '[N. CONTO]':             n_conto,
                '[Istituto bancario]':    istituto,
                '[ISTITUTO BANCARIO]':    istituto,
                '[DATA]':                 oggi,
                # riga tratteggiata prima della firma (es. "___________  DATA")
                '___________':            oggi,
            }

            docx_data = self._docx_replace_placeholders(
                config.accordo_retrocessioni_template, replacements)
            # Aggiunge la riga dati con 27 celle carattere nella tabella PAESE/CIN/ABI/CAB
            docx_data = self._docx_expand_iban_table(docx_data, iban)
            pdf_data = self._docx_to_pdf(docx_data)

            filename = 'Accordo amministratore.pdf'
            return request.make_response(
                pdf_data,
                headers=[
                    ('Content-Type', 'application/pdf'),
                    ('Content-Disposition', 'attachment; filename="%s"' % filename),
                    ('Content-Length', len(pdf_data)),
                ],
            )

        except Exception as e:
            _logger.error('BuildingPay: errore download accordo retrocessioni: %s', e)
            return request.redirect('/my/contratti?error=download_error')

    @http.route('/my/contratti/retrocessioni/download-custom', type='http', auth='user',
                website=True)
    def portal_retrocessioni_custom_download(self, **kw):
        """
        Download del file contratto retrocessioni personalizzato (dal referrer).
        Applica gli stessi placeholder del template standard più la data odierna,
        poi converte in PDF.

        Placeholder sostituiti (stessi del template standard):
        - [NOME AMMINISTRATORE] → partner.name
        - [CF] / [CODICE FISCALE] / [________] → partner.fiscalcode
        - [IBAN]                → IBAN completo
        - [Paese] [EUR] [CIN] [ABI] [CAB] [N.CONTO] → scomposizione IBAN
        - [Istituto bancario]   → banca del partner
        - [DATA] / ___________ → data odierna DD/MM/YYYY
        """
        partner = request.env.user.partner_id.sudo()
        referrer = partner.referrer_id
        if not referrer or not referrer.contratto_retrocessione_custom_file:
            return request.redirect('/my/contratti?error=no_template_retrocessioni')
        try:
            nome_amministratore = partner.name or ''
            codice_fiscale = partner.fiscalcode or ''

            bank = request.env['res.partner.bank'].sudo().search([
                ('partner_id', '=', partner.id),
            ], limit=1)

            iban_raw = (bank.acc_number if bank else '') or ''
            iban = iban_raw.replace(' ', '').upper()

            if len(iban) >= 5 and iban[:2].isalpha():
                paese   = iban[0:2]
                eur     = iban[2:4]
                cin     = iban[4:5]
                abi     = iban[5:10]  if len(iban) > 5  else ''
                cab     = iban[10:15] if len(iban) > 10 else ''
                n_conto = iban[15:]   if len(iban) > 15 else ''
            else:
                paese = eur = cin = abi = cab = n_conto = ''

            oggi = date.today().strftime('%d/%m/%Y')
            istituto = (bank.bank_id.name if bank and bank.bank_id else '') or ''
            intestatario = (bank.acc_holder_name or nome_amministratore) if bank else nome_amministratore

            replacements = {
                '[NOME AMMINISTRATORE]':  nome_amministratore,
                '[Intestatario]':         intestatario,
                '[INTESTATARIO]':         intestatario,
                '[CF]':                   codice_fiscale,
                '[CODICE FISCALE]':       codice_fiscale,
                '[________]':             codice_fiscale,
                '[IBAN]':                 iban,
                '[Paese]':                paese,
                '[PAESE]':                paese,
                '[EUR]':                  eur,
                '[CIN]':                  cin,
                '[ABI]':                  abi,
                '[CAB]':                  cab,
                '[N.CONTO]':              n_conto,
                '[N. CONTO]':             n_conto,
                '[Istituto bancario]':    istituto,
                '[ISTITUTO BANCARIO]':    istituto,
                '[DATA]':                 oggi,
                '___________':            oggi,
            }

            docx_data = self._docx_replace_placeholders(
                referrer.contratto_retrocessione_custom_file, replacements)
            docx_data = self._docx_expand_iban_table(docx_data, iban)
            pdf_data = self._docx_to_pdf(docx_data)

            filename = 'Accordo amministratore.pdf'
            return request.make_response(
                pdf_data,
                headers=[
                    ('Content-Type', 'application/pdf'),
                    ('Content-Disposition', 'attachment; filename="%s"' % filename),
                    ('Content-Length', len(pdf_data)),
                ],
            )
        except Exception as e:
            _logger.error('BuildingPay: errore download contratto custom retrocessioni: %s', e)
            return request.redirect('/my/contratti?error=download_error')

    @http.route('/my/contratti/retrocessioni/upload', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_retrocessioni_upload(self, **kw):
        """
        Upload del file 'Accordo Retrocessioni' firmato.
        Attiva il flag accordo_retrocessioni_ed sul partner.
        """
        partner = request.env.user.partner_id
        uploaded_file = kw.get('retrocessioni_file')

        if not uploaded_file:
            return request.redirect('/my/contratti?error=no_file')

        try:
            file_data = uploaded_file.read()
            filename = uploaded_file.filename
            file_b64 = base64.b64encode(file_data)
            partner.sudo().action_upload_retrocessioni(file_b64, filename)
            # Crea attività ToDo per gli assegnatari configurati
            self._create_accordo_admin_activity(partner)
            return request.redirect('/my/contratti?success=retrocessioni_uploaded')
        except Exception as e:
            _logger.error('BuildingPay: errore upload accordo retrocessioni: %s', e)
            return request.redirect('/my/contratti?error=upload_error')

    def _create_accordo_admin_activity(self, partner):
        """Crea un ToDo per ogni assegnatario configurato quando l'amministratore
        carica l'Accordo Amministratore firmato dal portale."""
        from datetime import date, timedelta
        try:
            config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()
            if not config or not config.create_activity_on_accordo_admin:
                return

            responsabili = [
                config.activity_accordo_admin_responsible_1_id,
                config.activity_accordo_admin_responsible_2_id,
                config.activity_accordo_admin_responsible_3_id,
                config.activity_accordo_admin_responsible_4_id,
            ]
            responsabili = [r for r in responsabili if r]
            if not responsabili:
                _logger.warning(
                    'BuildingPay: attività accordo admin non creata: nessun assegnatario configurato')
                return

            days = config.activity_accordo_admin_days or 1
            deadline = date.today() + timedelta(days=days)
            activity_type = request.env.ref(
                'mail.mail_activity_data_todo', raise_if_not_found=False)

            summary = 'Contratto amministratore caricato'
            note = 'Controllare il file Accordo Amministratore caricato e firmarlo'

            for user in responsabili:
                partner.sudo().activity_schedule(
                    activity_type_id=activity_type.id if activity_type else False,
                    summary=summary,
                    note=note,
                    date_deadline=deadline,
                    user_id=user.id,
                )
                _logger.info(
                    'BuildingPay: ToDo "Accordo admin caricato" creato per %s (assegnatario: %s)',
                    partner.name, user.name)
        except Exception as e:
            _logger.warning('BuildingPay: errore creazione attività accordo admin: %s', e)

    # -------------------------------------------------------
    # CONTRATTO 2: Accordo Condomini Aggregati ED
    # Download con placeholder: NOME AMMINISTRATORE, [________] (CF), [ALLEGATO_A]
    # [ALLEGATO_A] viene sostituito da una tabella con i condomini attivi
    # -------------------------------------------------------
    @http.route('/my/contratti/condomini-aggregati/download', type='http',
                auth='user', website=True)
    def portal_contratto_download(self, **kw):
        """
        Download del template 'Accordo Condomini Aggregati ED' in formato PDF.
        Placeholder testuali sostituiti:
        - [NOME AMMINISTRATORE] / [Nome Amministratore] → partner.name
        - [________]                                    → partner.fiscalcode
        - ___________  (data firma)                     → data odierna DD/MM/YYYY
        Allegato A: la tabella esistente nel template viene riempita con i
        condomini attivi (N. | Ubicazione | CF/P.IVA | IBAN | Data inserimento).
        """
        partner = request.env.user.partner_id.sudo()
        config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()

        if not config or not config.contratto_template:
            return request.redirect('/my/contratti?error=no_template_condomini')

        try:
            nome_amministratore = partner.name or ''
            codice_fiscale = partner.fiscalcode or ''
            oggi = date.today().strftime('%d/%m/%Y')

            condomini = request.env['res.partner'].sudo().search([
                ('parent_id', '=', partner.id),
                ('type', '=', 'condominio'),
                ('active', '=', True),
            ], order='name asc')

            replacements = {
                '[NOME AMMINISTRATORE]': nome_amministratore,
                '[Nome Amministratore]': nome_amministratore,
                '[________]':            codice_fiscale,
                '___________':           oggi,
            }

            # 1. Sostituzione placeholder testuali
            file_data = self._docx_replace_placeholders(
                config.contratto_template, replacements)
            # 2. Riempimento tabella Allegato A con i condomini attivi
            file_data = self._docx_fill_allegato_a_table(file_data, condomini)
            # 3. Conversione in PDF
            pdf_data = self._docx_to_pdf(file_data)

            filename = 'Accordo Condomini Aggregati.pdf'
            return request.make_response(
                pdf_data,
                headers=[
                    ('Content-Type', 'application/pdf'),
                    ('Content-Disposition',
                     'attachment; filename="%s"' % filename),
                    ('Content-Length', len(pdf_data)),
                ],
            )

        except Exception as e:
            _logger.error('BuildingPay: errore download accordo condomini aggregati: %s', e)
            return request.redirect('/my/contratti?error=download_error')

    # -------------------------------------------------------
    # Helper: manipolazione .docx senza python-docx
    # Usa solo zipfile (stdlib) + lxml (sempre disponibile in Odoo)
    # -------------------------------------------------------

    _W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    def _docx_replace_placeholders(self, template_b64, replacements):
        """
        Apre il .docx (base64) come ZIP, sostituisce i placeholder
        nel document.xml usando lxml, restituisce i bytes modificati.
        Non richiede python-docx.
        """
        import zipfile
        from lxml import etree

        raw = base64.b64decode(template_b64)
        in_buf = BytesIO(raw)
        out_buf = BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin:
            with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == 'word/document.xml':
                        data = self._docx_xml_replace(data, replacements)
                    zout.writestr(item, data)

        out_buf.seek(0)
        return out_buf.read()

    def _docx_xml_replace(self, xml_bytes, replacements):
        """
        Itera i paragrafi (w:p) del document.xml.
        Per ogni paragrafo, raccoglie il testo completo da tutti i w:r/w:t,
        applica le sostituzioni, mette il risultato nel primo w:t e svuota gli altri.
        Gestisce run spezzati (Word split runs).
        """
        from lxml import etree

        W = self._W
        root = etree.fromstring(xml_bytes)

        for para in root.iter('{%s}p' % W):
            runs = para.findall('.//{%s}r' % W)
            if not runs:
                continue

            # Testo completo del paragrafo (tutti i run)
            full_text = ''
            for run in runs:
                t = run.find('{%s}t' % W)
                full_text += (t.text or '') if t is not None else ''

            if not any(ph in full_text for ph in replacements):
                continue

            new_text = full_text
            for ph, val in replacements.items():
                new_text = new_text.replace(ph, val)

            # Scrivi il nuovo testo nel primo run, svuota gli altri
            first_t = runs[0].find('{%s}t' % W)
            if first_t is None:
                first_t = etree.SubElement(runs[0], '{%s}t' % W)
            first_t.text = new_text
            if new_text and (new_text[0] == ' ' or new_text[-1] == ' '):
                first_t.set(
                    '{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            for run in runs[1:]:
                t = run.find('{%s}t' % W)
                if t is not None:
                    t.text = ''

        return etree.tostring(root, xml_declaration=True,
                              encoding='UTF-8', standalone=True)

    def _docx_insert_allegato_a(self, docx_bytes, condomini):
        """
        Trova il paragrafo che contiene [ALLEGATO_A] nel document.xml
        e lo sostituisce con una tabella Word a 3 colonne
        (Denominazione | Indirizzo | IBAN).
        """
        import zipfile
        from lxml import etree

        W = self._W

        in_buf = BytesIO(docx_bytes)
        out_buf = BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin:
            with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == 'word/document.xml':
                        root = etree.fromstring(data)
                        self._docx_xml_insert_table(root, condomini, W)
                        data = etree.tostring(
                            root, xml_declaration=True,
                            encoding='UTF-8', standalone=True)
                    zout.writestr(item, data)

        out_buf.seek(0)
        return out_buf.read()

    def _docx_xml_insert_table(self, root, condomini, W):
        """
        Sostituisce il w:p contenente [ALLEGATO_A] con un w:tbl Word.
        """
        from lxml import etree

        target_para = None
        for para in root.iter('{%s}p' % W):
            runs = para.findall('.//{%s}r' % W)
            text = ''.join(
                (r.find('{%s}t' % W).text or '')
                for r in runs if r.find('{%s}t' % W) is not None
            )
            if '[ALLEGATO_A]' in text:
                target_para = para
                break

        if target_para is None:
            return  # placeholder non trovato, non toccare il documento

        # Costruisce la tabella come XML Word
        tbl_xml = self._build_allegato_a_xml(condomini, W)
        tbl_el = etree.fromstring(tbl_xml)

        parent = target_para.getparent()
        idx = list(parent).index(target_para)
        parent.remove(target_para)
        parent.insert(idx, tbl_el)

    def _build_allegato_a_xml(self, condomini, W):
        """
        Genera il markup XML (stringa bytes) di una w:tbl Word con:
        - riga di intestazione in grassetto: Denominazione | Indirizzo | IBAN
        - una riga per ogni condominio attivo
        """
        def esc(s):
            return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        def cell(text, bold=False):
            b_open = '<w:b/>' if bold else ''
            return (
                '<w:tc>'
                '<w:p><w:r>'
                '<w:rPr>%s</w:rPr>'
                '<w:t xml:space="preserve">%s</w:t>'
                '</w:r></w:p>'
                '</w:tc>'
            ) % (b_open, esc(text))

        rows_xml = ''
        # Intestazione
        rows_xml += '<w:tr>%s%s%s</w:tr>' % (
            cell('Denominazione Condominio', bold=True),
            cell('Indirizzo', bold=True),
            cell('IBAN', bold=True),
        )
        # Righe dati
        for condo in condomini:
            bank = request.env['res.partner.bank'].sudo().search(
                [('partner_id', '=', condo.id)], limit=1)
            parts = [p for p in [
                condo.street or '',
                condo.zip or '',
                condo.city or '',
                condo.state_id.name if condo.state_id else '',
            ] if p]
            address = ' '.join(parts)
            iban_val = bank.acc_number if bank else ''
            rows_xml += '<w:tr>%s%s%s</w:tr>' % (
                cell(condo.name or ''),
                cell(address),
                cell(iban_val),
            )

        tbl = (
            '<w:tbl xmlns:w="%(W)s">'
            '<w:tblPr>'
            '<w:tblStyle w:val="TableGrid"/>'
            '<w:tblW w:w="0" w:type="auto"/>'
            '</w:tblPr>'
            '<w:tblGrid>'
            '<w:gridCol/><w:gridCol/><w:gridCol/>'
            '</w:tblGrid>'
            '%(rows)s'
            '</w:tbl>'
        ) % {'W': W, 'rows': rows_xml}

        return tbl.encode('utf-8')

    # -------------------------------------------------------
    # Riempimento tabella Allegato A nel nuovo template
    # Il template ha già la tabella formattata con 80 righe.
    # Troviamo la tabella, rimuoviamo le righe dati, inseriamo
    # una riga per ciascun condominio attivo clonando la struttura
    # della prima riga dati (preserva bordi e formattazione).
    # -------------------------------------------------------

    def _docx_fill_allegato_a_table(self, docx_bytes, condomini):
        """
        Apre il docx come ZIP, trova la tabella Allegato A identificata
        dall'intestazione 'Ubicazione', rimuove tutte le righe dati e
        le ricrea con i dati dei condomini attivi.
        Restituisce i bytes del docx modificato.
        """
        import zipfile
        from lxml import etree

        W = self._W
        in_buf = BytesIO(docx_bytes)
        out_buf = BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin:
            with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == 'word/document.xml':
                        root = etree.fromstring(data)
                        self._docx_xml_fill_allegato_a(root, condomini, W)
                        data = etree.tostring(
                            root, xml_declaration=True,
                            encoding='UTF-8', standalone=True)
                    zout.writestr(item, data)

        out_buf.seek(0)
        return out_buf.read()

    def _docx_xml_fill_allegato_a(self, root, condomini, W):
        """
        Individua la tabella con 'Ubicazione' nell'intestazione,
        rimuove tutte le righe dati (mantenendo solo il header),
        poi aggiunge una riga per ogni condominio clonando la
        struttura della prima riga dati originale.
        Colonne: N. | Ubicazione Condominiale | CF/P.IVA | IBAN | Data inserimento
        - IBAN senza spazi
        - Font 9pt (18 half-points) per compattezza
        - 2 righe vuote dopo i dati
        """
        import copy

        # --- Trova la tabella Allegato A ---
        allegato_tbl = None
        for tbl in root.iter('{%s}tbl' % W):
            rows = tbl.findall('{%s}tr' % W)
            if not rows:
                continue
            header_text = ''.join(
                (t.text or '') for t in rows[0].iter('{%s}t' % W))
            if 'Ubicazione' in header_text:
                allegato_tbl = tbl
                break

        if allegato_tbl is None:
            return

        rows = allegato_tbl.findall('{%s}tr' % W)
        # La prima riga dati (indice 1) viene usata come template di formattazione
        template_row = rows[1] if len(rows) > 1 else None

        # Rimuovi tutte le righe tranne l'header
        for row in rows[1:]:
            allegato_tbl.remove(row)

        if not condomini:
            return

        # Pre-carica tutti gli IBAN in un'unica query
        bank_map = {}
        banks = request.env['res.partner.bank'].sudo().search(
            [('partner_id', 'in', condomini.ids)])
        for b in banks:
            if b.partner_id.id not in bank_map:
                bank_map[b.partner_id.id] = b.acc_number or ''

        for i, condo in enumerate(condomini, start=1):
            # Indirizzo completo
            addr_parts = [p for p in [
                condo.street or '',
                (condo.zip or '') + ' ' + (condo.city or '') if condo.city else (condo.zip or ''),
                ('(' + condo.state_id.code + ')') if condo.state_id else '',
            ] if p.strip()]
            address = ', '.join(addr_parts)
            ubicazione = (condo.name or '') + (', ' + address if address else '')

            # CF / P.IVA
            cf_piva_parts = []
            if condo.fiscalcode:
                cf_piva_parts.append(condo.fiscalcode)
            if condo.vat:
                cf_piva_parts.append(condo.vat)
            cf_piva = ' / '.join(cf_piva_parts)

            # IBAN senza spazi
            iban_val = bank_map.get(condo.id, '').replace(' ', '')

            # Crea la riga clonando il template (preserva bordi/formattazione)
            if template_row is not None:
                new_row = copy.deepcopy(template_row)
                self._docx_set_row_cells(
                    new_row, [str(i), ubicazione, cf_piva, iban_val, ''], W)
            else:
                new_row = self._docx_build_plain_row(
                    [str(i), ubicazione, cf_piva, iban_val, ''], W)

            allegato_tbl.append(new_row)

        # Aggiungi 2 righe vuote dopo i dati
        for _ in range(2):
            if template_row is not None:
                empty_row = copy.deepcopy(template_row)
                self._docx_set_row_cells(empty_row, ['', '', '', '', ''], W)
            else:
                empty_row = self._docx_build_plain_row(['', '', '', '', ''], W)
            allegato_tbl.append(empty_row)

    def _docx_set_row_cells(self, row, values, W, font_size_half_pt=18):
        """
        Imposta il testo delle celle in una riga clonata e riduce
        il font a font_size_half_pt (default 18 = 9pt).
        Trova tutti i w:t per cella, scrive il valore nel primo,
        svuota gli altri (gestisce run multipli dal template).
        """
        from lxml import etree

        cells = row.findall('{%s}tc' % W)
        for cell, val in zip(cells, values):
            all_t = cell.findall('.//{%s}t' % W)
            if all_t:
                all_t[0].text = val or ''
                if val and (val[0] == ' ' or val[-1] == ' '):
                    all_t[0].set(
                        '{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                for t in all_t[1:]:
                    t.text = ''

            # Imposta dimensione font su tutti i run della cella
            for r in cell.findall('.//{%s}r' % W):
                rpr = r.find('{%s}rPr' % W)
                if rpr is None:
                    rpr = etree.Element('{%s}rPr' % W)
                    r.insert(0, rpr)
                for tag in ('sz', 'szCs'):
                    el = rpr.find('{%s}%s' % (W, tag))
                    if el is None:
                        el = etree.SubElement(rpr, '{%s}%s' % (W, tag))
                    el.set('{%s}val' % W, str(font_size_half_pt))

    def _docx_build_plain_row(self, values, W, font_size_half_pt=18):
        """
        Costruisce una w:tr minimale (fallback se il template non ha righe dati).
        """
        from lxml import etree

        def esc(s):
            return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        cells_xml = ''.join(
            '<w:tc xmlns:w="{W}"><w:p><w:r>'
            '<w:rPr>'
            '<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>'
            '</w:rPr>'
            '<w:t xml:space="preserve">{v}</w:t>'
            '</w:r></w:p></w:tc>'.format(W=W, v=esc(v), sz=font_size_half_pt)
            for v in values
        )
        tr_xml = '<w:tr xmlns:w="{W}">{cells}</w:tr>'.format(W=W, cells=cells_xml)
        return etree.fromstring(tr_xml.encode('utf-8'))

    def _docx_to_pdf(self, docx_bytes):
        """
        Converte un .docx (bytes) in PDF usando LibreOffice headless.
        LibreOffice è disponibile in tutti gli ambienti Odoo standard (incluso Odoo.sh).
        Lancia: libreoffice --headless --convert-to pdf --outdir <tmpdir> <file.docx>
        """
        import subprocess
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, 'document.docx')
            pdf_path = os.path.join(tmpdir, 'document.pdf')

            with open(docx_path, 'wb') as fh:
                fh.write(docx_bytes)

            # LibreOffice headless conversion
            result = subprocess.run(
                [
                    'libreoffice', '--headless', '--norestore',
                    '--convert-to', 'pdf',
                    '--outdir', tmpdir,
                    docx_path,
                ],
                capture_output=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    'LibreOffice conversion failed: %s' %
                    result.stderr.decode('utf-8', errors='replace'))

            with open(pdf_path, 'rb') as fh:
                return fh.read()

    def _docx_expand_iban_table(self, docx_bytes, iban):
        """
        Trova la tabella PAESE/CIN/ABI/CAB nel documento e:
        1. Aggiorna tblGrid a 27 colonne di larghezza proporzionale
        2. Aggiunge gridSpan alle celle header (2,2,1,5,5,12)
        3. Aggiunge una riga dati con 27 celle (una per carattere IBAN)
        Larghezze calibrate sul template originale (total 9971 twips).
        """
        import zipfile
        from lxml import etree

        W = self._W
        in_buf = BytesIO(docx_bytes)
        out_buf = BytesIO()

        with zipfile.ZipFile(in_buf, 'r') as zin:
            with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == 'word/document.xml':
                        root = etree.fromstring(data)
                        self._xml_add_iban_char_row(root, iban, W)
                        data = etree.tostring(
                            root, xml_declaration=True,
                            encoding='UTF-8', standalone=True)
                    zout.writestr(item, data)

        out_buf.seek(0)
        return out_buf.read()

    def _xml_add_iban_char_row(self, root, iban, W):
        """
        Modifica in-place la tabella PAESE/ABI/CAB nel documento XML.
        Larghezze per carattere (twips), totale 9971:
          PAESE(2): 388+388
          CIN+EUR(2): 452+452
          CIN(1): 435
          ABI(5): 357×4+358
          CAB(5): 357×5
          N.CONTO(12): 357×11+358
        """
        from lxml import etree

        # Larghezze per ciascuno dei 27 caratteri (somma = 9971)
        CHAR_WIDTHS = (
            [388, 388] +                           # PAESE 2 chars
            [452, 452] +                           # CIN+EUR 2 chars
            [435] +                                # CIN 1 char
            [357, 357, 357, 357, 358] +            # ABI 5 chars
            [357, 357, 357, 357, 357] +            # CAB 5 chars
            [357, 357, 357, 357, 357, 357,
             357, 357, 357, 357, 357, 358]         # N.CONTO 12 chars
        )
        # Numero di colonne del grid coperte da ciascuna cella header
        HEADER_SPANS = [2, 2, 1, 5, 5, 12]

        body = root.find('.//{%s}body' % W)
        for tbl in body.findall('{%s}tbl' % W):
            rows = tbl.findall('{%s}tr' % W)
            if not rows:
                continue
            hdr_text = ''.join(
                (t.text or '') for t in rows[0].iter('{%s}t' % W))
            if 'PAESE' not in hdr_text or 'ABI' not in hdr_text:
                continue

            # 1. Aggiorna tblGrid → 27 colonne
            tbl_grid = tbl.find('{%s}tblGrid' % W)
            if tbl_grid is None:
                tbl_grid = etree.Element('{%s}tblGrid' % W)
                tbl.insert(1, tbl_grid)  # dopo tblPr
            for gc in list(tbl_grid.findall('{%s}gridCol' % W)):
                tbl_grid.remove(gc)
            for w in CHAR_WIDTHS:
                gc = etree.SubElement(tbl_grid, '{%s}gridCol' % W)
                gc.set('{%s}w' % W, str(w))

            # 2. Aggiunge gridSpan alle celle header
            hdr_cells = rows[0].findall('{%s}tc' % W)
            for cell, span in zip(hdr_cells, HEADER_SPANS):
                if span == 1:
                    continue
                tcpr = cell.find('{%s}tcPr' % W)
                if tcpr is None:
                    tcpr = etree.Element('{%s}tcPr' % W)
                    cell.insert(0, tcpr)
                gs = tcpr.find('{%s}gridSpan' % W)
                if gs is None:
                    gs = etree.Element('{%s}gridSpan' % W)
                    tcw = tcpr.find('{%s}tcW' % W)
                    if tcw is not None:
                        tcw.addnext(gs)
                    else:
                        tcpr.append(gs)
                gs.set('{%s}val' % W, str(span))

            # 3. Crea riga dati con 27 celle
            iban_chars = list(iban[:27].ljust(27))

            tr_el = etree.Element('{%s}tr' % W)
            trpr = etree.SubElement(tr_el, '{%s}trPr' % W)
            trh = etree.SubElement(trpr, '{%s}trHeight' % W)
            trh.set('{%s}val' % W, '340')

            for char, cell_w in zip(iban_chars, CHAR_WIDTHS):
                tc = etree.SubElement(tr_el, '{%s}tc' % W)
                # Proprietà cella
                tcpr = etree.SubElement(tc, '{%s}tcPr' % W)
                tcw_el = etree.SubElement(tcpr, '{%s}tcW' % W)
                tcw_el.set('{%s}w' % W, str(cell_w))
                tcw_el.set('{%s}type' % W, 'dxa')
                # Bordi singoli su tutti i lati
                borders = etree.SubElement(tcpr, '{%s}tcBorders' % W)
                for side in ('top', 'left', 'bottom', 'right'):
                    b = etree.SubElement(borders, '{%s}%s' % (W, side))
                    b.set('{%s}val' % W, 'single')
                    b.set('{%s}sz' % W, '8')
                    b.set('{%s}space' % W, '0')
                    b.set('{%s}color' % W, '000000')
                # Paragrafo centrato
                p = etree.SubElement(tc, '{%s}p' % W)
                ppr = etree.SubElement(p, '{%s}pPr' % W)
                ps = etree.SubElement(ppr, '{%s}pStyle' % W)
                ps.set('{%s}val' % W, 'TableParagraph')
                jc = etree.SubElement(ppr, '{%s}jc' % W)
                jc.set('{%s}val' % W, 'center')
                ppr_rpr = etree.SubElement(ppr, '{%s}rPr' % W)
                for rpr_el in [ppr_rpr]:
                    rf = etree.SubElement(rpr_el, '{%s}rFonts' % W)
                    rf.set('{%s}ascii' % W, 'Calibri')
                    rf.set('{%s}hAnsi' % W, 'Calibri')
                    rf.set('{%s}cs' % W, 'Calibri')
                    etree.SubElement(rpr_el, '{%s}sz' % W).set('{%s}val' % W, '18')
                    etree.SubElement(rpr_el, '{%s}szCs' % W).set('{%s}val' % W, '18')
                # Run con il carattere
                r_el = etree.SubElement(p, '{%s}r' % W)
                rpr_r = etree.SubElement(r_el, '{%s}rPr' % W)
                rf2 = etree.SubElement(rpr_r, '{%s}rFonts' % W)
                rf2.set('{%s}ascii' % W, 'Calibri')
                rf2.set('{%s}hAnsi' % W, 'Calibri')
                rf2.set('{%s}cs' % W, 'Calibri')
                etree.SubElement(rpr_r, '{%s}sz' % W).set('{%s}val' % W, '18')
                etree.SubElement(rpr_r, '{%s}szCs' % W).set('{%s}val' % W, '18')
                t_el = etree.SubElement(r_el, '{%s}t' % W)
                t_el.text = char
                if char == ' ':
                    t_el.set(
                        '{http://www.w3.org/XML/1998/namespace}space', 'preserve')

            tbl.append(tr_el)
            break  # trovata e modificata, esci

    @http.route('/my/contratti/condomini-aggregati/upload', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_contratto_upload(self, **kw):
        """
        Upload del file 'Accordo Condomini Aggregati ED' firmato.
        Attiva il flag accordo_condomini_aggregati_ed sul partner.
        """
        partner = request.env.user.partner_id
        uploaded_file = kw.get('contratto_file')

        if not uploaded_file:
            return request.redirect('/my/contratti?error=no_file')

        try:
            file_data = uploaded_file.read()
            filename = uploaded_file.filename
            file_b64 = base64.b64encode(file_data)
            partner.sudo().action_upload_accordo_condomini(file_b64, filename)
            return request.redirect('/my/contratti?success=condomini_uploaded')
        except Exception as e:
            _logger.error('BuildingPay: errore upload accordo condomini aggregati: %s', e)
            return request.redirect('/my/contratti?error=upload_error')

    # -------------------------------------------------------
    # SEZIONE: Profilo amministratore (CF, IBAN, banca, note)
    # -------------------------------------------------------
    @http.route('/my/profilo', type='http', auth='user', website=True)
    def portal_profilo(self, **kw):
        """Pagina dati personali dell'amministratore."""
        # sudo() necessario: senza di esso campi custom (fiscalcode, comment)
        # possono risultare vuoti per i record accessibili al solo utente portale.
        partner = request.env.user.partner_id.sudo()
        if not partner.is_amministratore:
            return request.redirect('/my/home')
        bank = request.env['res.partner.bank'].sudo().search(
            [('partner_id', '=', partner.id)], limit=1)
        return request.render('BuildingPay_onboarding_v15.portal_profilo', {
            'partner': partner,
            'bank': bank,
            'note_plain': self._html_to_text(partner.comment),
            'page_name': 'profilo',
        })

    @http.route('/my/profilo/save', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_profilo_save(self, **kw):
        """
        Salva i dati personali dell'amministratore:
        CF, P.IVA, PEC, Nome Promotore, Applicativo,
        Sistema di pagamento, IBAN, Note.
        """
        from odoo.addons.base_iban.models.res_partner_bank import validate_iban
        from odoo.exceptions import ValidationError

        partner = request.env.user.partner_id
        if not partner.is_amministratore:
            return request.redirect('/my/home')
        try:
            params = request.params

            # ----------------------------------------------------------
            # Validazione IBAN (se presente) prima di qualunque scrittura
            # ----------------------------------------------------------
            iban = params.get('iban', '').replace(' ', '').upper().strip()
            if iban:
                try:
                    validate_iban(iban)
                except ValidationError:
                    return request.redirect('/my/profilo?error=iban_invalid')

            # ----------------------------------------------------------
            # Dati anagrafici / operativi
            # ----------------------------------------------------------
            partner_vals = {
                'comment': params.get('note', '').strip() or False,
            }

            # Campi semplici: scriviamo il valore se presente,
            # altrimenti lasciamo invariato (False svuota il campo).
            for field in ['fiscalcode', 'pec_mail', 'nome_promotore',
                          'applicativo', 'sistema_pagamento']:
                val = params.get(field, '').strip()
                partner_vals[field] = val or False

            # Partita IVA: usa savepoint isolato perché Odoo può
            # applicare una constraint di formato (es. IT + 11 cifre).
            vat = params.get('vat', '').strip()
            if vat:
                try:
                    with request.env.cr.savepoint():
                        partner.sudo().write({'vat': vat})
                except Exception as e:
                    _logger.warning('BuildingPay profilo – P.IVA non valida (%s): %s', vat, e)
            else:
                partner_vals['vat'] = False

            partner.sudo().write(partner_vals)

            # ----------------------------------------------------------
            # IBAN → res.partner.bank
            # base_bank_from_iban.create() imposta bank_id automaticamente
            # dall'IBAN tramite schwifty (no banca_id manuale necessario).
            # ----------------------------------------------------------
            existing_bank = request.env['res.partner.bank'].sudo().search(
                [('partner_id', '=', partner.id)], limit=1)

            if iban:
                if existing_bank:
                    existing_bank.sudo().write({'acc_number': iban})
                else:
                    request.env['res.partner.bank'].sudo().create({
                        'partner_id': partner.id,
                        'acc_number': iban,
                    })

            return request.redirect('/my/profilo?success=1')
        except Exception as e:
            _logger.error('BuildingPay: errore salvataggio profilo: %s', e)
            return request.redirect('/my/profilo?error=1')

    # -------------------------------------------------------
    # SEZIONE: Condomini
    # -------------------------------------------------------
    @http.route('/my/condomini', type='http', auth='user', website=True)
    def portal_condomini_list(self, page=1, **kw):
        """Lista dei condomini dell'amministratore."""
        partner = request.env.user.partner_id

        if not partner.is_amministratore:
            return request.redirect('/my')

        domain = [
            ('parent_id', '=', partner.id),
            ('type', '=', 'condominio'),
            ('active', '=', True),
        ]

        condomini = request.env['res.partner'].sudo().search(domain)

        values = {
            'partner': partner,
            'condomini': condomini,
            'is_validato': partner.sudo().is_amministratore_validato,
            'page_name': 'condomini',
        }
        return request.render('BuildingPay_onboarding_v15.portal_condomini', values)

    @http.route('/my/condomini/new', type='http', auth='user', website=True)
    def portal_condominio_new(self, **kw):
        """Form per aggiungere un nuovo condominio."""
        partner = request.env.user.partner_id

        if not partner.is_amministratore:
            return request.redirect('/my')

        # Blocco: l'amministratore non è ancora stato validato dall'operatore
        if not partner.sudo().is_amministratore_validato:
            return request.redirect('/my/condomini?error=not_validated')

        countries = request.env['res.country'].sudo().search([])
        banks = request.env['res.bank'].sudo().search([], order='name')
        values = {
            'partner': partner,
            'condominio': None,
            'countries': countries,
            'banks': banks,
            'bank': None,
            'note_plain': '',
            'page_name': 'condomini_new',
            'mode': 'create',
        }
        return request.render('BuildingPay_onboarding_v15.portal_condominio_form', values)

    @http.route('/my/condomini/new/save', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_condominio_create(self, **kw):
        """Salva un nuovo indirizzo condominio."""
        partner = request.env.user.partner_id

        if not partner.is_amministratore:
            return request.redirect('/my')

        # Blocco POST: doppio controllo server-side (non aggirabile via form diretto)
        if not partner.sudo().is_amministratore_validato:
            return request.redirect('/my/condomini?error=not_validated')

        params = request.params
        errors = self._validate_condominio_form(params)

        if errors:
            countries = request.env['res.country'].sudo().search([])
            banks = request.env['res.bank'].sudo().search([], order='name')
            return request.render('BuildingPay_onboarding_v15.portal_condominio_form', {
                'partner': partner,
                'condominio': None,
                'countries': countries,
                'banks': banks,
                'bank': None,
                'note_plain': '',
                'errors': errors,
                'form_data': params,
                'mode': 'create',
                'page_name': 'condomini_new',
            })

        try:
            condominio_vals = self._prepare_condominio_vals(params, partner)
            condominio = request.env['res.partner'].sudo().create(condominio_vals)

            # Salva IBAN e istituto bancario
            iban = params.get('iban', '').strip()
            banca_id_raw = params.get('banca_id', '').strip()
            banca_id = int(banca_id_raw) if banca_id_raw else False
            if iban:
                bank_vals = {
                    'partner_id': condominio.id,
                    'acc_number': iban,
                }
                if banca_id:
                    bank_vals['bank_id'] = banca_id
                request.env['res.partner.bank'].sudo().create(bank_vals)

            # Attiva flag electronic invoice se codice destinatario presente
            if params.get('codice_destinatario'):
                condominio.sudo().write({
                    'electronic_invoice_subjected': True,
                    'electronic_invoice_obliged_subject': True,
                })

            return request.redirect('/my/condomini?success_add=1')
        except Exception as e:
            _logger.error('BuildingPay: errore creazione condominio: %s', e)
            return request.redirect('/my/condomini?error=create_error')

    @http.route('/my/condomini/<int:condominio_id>', type='http', auth='user', website=True)
    def portal_condominio_detail(self, condominio_id, **kw):
        """Dettaglio/modifica di un condominio esistente."""
        partner = request.env.user.partner_id
        condominio = self._get_condominio_or_redirect(condominio_id, partner)
        if isinstance(condominio, type(request.redirect('/'))):
            return condominio

        # Recupera IBAN dal conto bancario
        bank = request.env['res.partner.bank'].sudo().search([
            ('partner_id', '=', condominio.id),
        ], limit=1)

        countries = request.env['res.country'].sudo().search([])
        banks = request.env['res.bank'].sudo().search([], order='name')
        values = {
            'partner': partner,
            'condominio': condominio,
            'bank': bank,
            'countries': countries,
            'banks': banks,
            'note_plain': self._html_to_text(condominio.sudo().comment),
            'page_name': 'condomini_edit',
            'mode': 'edit',
        }
        return request.render('BuildingPay_onboarding_v15.portal_condominio_form', values)

    @http.route('/my/condomini/<int:condominio_id>/save', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_condominio_update(self, condominio_id, **kw):
        """Aggiorna un condominio esistente."""
        partner = request.env.user.partner_id
        condominio = self._get_condominio_or_redirect(condominio_id, partner)
        if isinstance(condominio, type(request.redirect('/'))):
            return condominio

        params = request.params
        errors = self._validate_condominio_form(params, exclude_id=condominio_id)

        if errors:
            countries = request.env['res.country'].sudo().search([])
            banks = request.env['res.bank'].sudo().search([], order='name')
            bank = request.env['res.partner.bank'].sudo().search([
                ('partner_id', '=', condominio.id),
            ], limit=1)
            return request.render('BuildingPay_onboarding_v15.portal_condominio_form', {
                'partner': partner,
                'condominio': condominio,
                'bank': bank,
                'countries': countries,
                'banks': banks,
                'note_plain': self._html_to_text(condominio.sudo().comment),
                'errors': errors,
                'form_data': params,
                'mode': 'edit',
                'page_name': 'condomini_edit',
            })

        try:
            condominio_vals = self._prepare_condominio_vals(params, partner)
            # Non sovrascriviamo parent_id e type in aggiornamento
            condominio_vals.pop('parent_id', None)
            condominio_vals.pop('type', None)
            condominio.sudo().write(condominio_vals)

            # Aggiorna IBAN e istituto bancario
            iban = params.get('iban', '').strip()
            banca_id_raw = params.get('banca_id', '').strip()
            banca_id = int(banca_id_raw) if banca_id_raw else False
            existing_bank = request.env['res.partner.bank'].sudo().search([
                ('partner_id', '=', condominio.id),
            ], limit=1)
            if iban:
                bank_vals = {'acc_number': iban}
                if banca_id:
                    bank_vals['bank_id'] = banca_id
                if existing_bank:
                    existing_bank.sudo().write(bank_vals)
                else:
                    bank_vals['partner_id'] = condominio.id
                    request.env['res.partner.bank'].sudo().create(bank_vals)
            elif existing_bank and banca_id:
                existing_bank.sudo().write({'bank_id': banca_id})

            # Aggiorna flag electronic invoice
            if params.get('codice_destinatario'):
                condominio.sudo().write({
                    'electronic_invoice_subjected': True,
                    'electronic_invoice_obliged_subject': True,
                })

            return request.redirect('/my/condomini?success_edit=1')
        except Exception as e:
            _logger.error('BuildingPay: errore aggiornamento condominio: %s', e)
            return request.redirect('/my/condomini?error=update_error')

    @http.route('/my/condomini/<int:condominio_id>/archive', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_condominio_archive(self, condominio_id, **kw):
        """Archivia un condominio (lo rende non attivo)."""
        partner = request.env.user.partner_id
        condominio = self._get_condominio_or_redirect(condominio_id, partner)
        if isinstance(condominio, type(request.redirect('/'))):
            return condominio

        try:
            condominio.sudo().action_archive_condominio()
            return request.redirect('/my/condomini?success_archive=1')
        except Exception as e:
            _logger.error('BuildingPay: errore archiviazione condominio: %s', e)
            return request.redirect('/my/condomini?error=archive_error')

    # -------------------------------------------------------
    # Metodi di utilità
    # -------------------------------------------------------

    @staticmethod
    def _html_to_text(html_val):
        """
        Converte un valore Html (campo Odoo) in testo piano per display
        in un elemento <textarea> del portale.
        I campi Html di Odoo (es. partner.comment) contengono markup
        come <p>…</p>: mostrarli direttamente in un textarea esporrebbe
        i tag raw all'utente.
        """
        import re
        if not html_val:
            return ''
        # Sostituisce <br> e </p> con newline prima di eliminare tutti i tag
        text = re.sub(r'<br\s*/?>', '\n', html_val)
        text = re.sub(r'</p>', '\n', text)
        text = re.sub(r'<[^>]+>', '', text)
        # Decodifica entità HTML di base
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
        return text.strip()

    def _get_condominio_or_redirect(self, condominio_id, partner):
        """
        Verifica che il condominio esista e appartenga all'utente corrente.
        Ritorna il record condominio oppure un redirect.
        """
        condominio = request.env['res.partner'].sudo().browse(condominio_id)
        if (not condominio.exists() or
                condominio.parent_id.id != partner.id or
                condominio.type != 'condominio'):
            return request.redirect('/my/condomini')
        return condominio

    def _validate_condominio_form(self, params, exclude_id=None):
        """Valida i dati del form condominio. Ritorna dict degli errori.
        exclude_id: ID del condominio corrente (per edit), escluso dalla ricerca duplicati CF.
        """
        errors = {}
        if not params.get('name', '').strip():
            errors['name'] = _('Il nome è obbligatorio.')
        if not params.get('street', '').strip():
            errors['street'] = _("L'indirizzo è obbligatorio.")
        if not params.get('city', '').strip():
            errors['city'] = _('La città è obbligatoria.')
        if not params.get('zip', '').strip():
            errors['zip'] = _('Il CAP è obbligatorio.')

        # Codice fiscale obbligatorio + controllo duplicato
        fiscalcode = params.get('fiscalcode', '').strip().upper()
        if not fiscalcode:
            errors['fiscalcode'] = _('Il codice fiscale è obbligatorio.')
        else:
            domain = [('fiscalcode', '=ilike', fiscalcode)]
            if exclude_id:
                domain.append(('id', '!=', exclude_id))
            existing_cf = request.env['res.partner'].sudo().search(domain, limit=1)
            if existing_cf:
                errors['fiscalcode'] = _(
                    'Il Codice Fiscale "%s" è già presente nel sistema. '
                    'Se il condominio è già registrato, contatta l\'assistenza.'
                ) % fiscalcode

        # IBAN obbligatorio + validazione formale
        iban = params.get('iban', '').replace(' ', '').upper().strip()
        if not iban:
            errors['iban'] = _('L\'IBAN è obbligatorio.')
        elif validate_iban:
            try:
                validate_iban(iban)
            except Exception:
                errors['iban'] = _('IBAN non valido. Verificare il codice inserito.')

        return errors

    def _prepare_condominio_vals(self, params, parent_partner):
        """Prepara il dict dei valori per creare/aggiornare un condominio."""
        vals = {
            'name': params.get('name', '').strip(),
            'type': 'condominio',
            'parent_id': parent_partner.id,
            'street': params.get('street', '').strip(),
            'street2': params.get('street2', '').strip(),
            'city': params.get('city', '').strip(),
            'zip': params.get('zip', '').strip(),
            'fiscalcode': params.get('fiscalcode', '').strip(),
            'pec_mail': params.get('pec_mail', '').strip(),
            'codice_destinatario': params.get('codice_destinatario', '').strip(),
            'comment': params.get('note', '').strip() or False,
        }
        country_id = params.get('country_id')
        if country_id:
            vals['country_id'] = int(country_id)
        state_id = params.get('state_id')
        if state_id:
            vals['state_id'] = int(state_id)
        return vals


