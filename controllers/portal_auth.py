# -*- coding: utf-8 -*-
import logging
import re
from odoo import http, fields as odoo_fields, _
from odoo.http import request
from odoo.exceptions import ValidationError
from odoo.addons.auth_signup.controllers.main import AuthSignupHome
from odoo.addons.base_iban.models.res_partner_bank import validate_iban

_logger = logging.getLogger(__name__)


def _normalize_vat_prefix(env, vat, country_id_val):
    """Aggiunge il prefisso IT alla P.IVA se il paese è Italia e il prefisso manca.
    Un prefisso è considerato già presente se i primi due caratteri sono entrambe lettere
    (es. 'IT', 'DE', 'FR', …). In quel caso il valore non viene modificato.
    Per paesi diversi dall'Italia il valore viene restituito invariato.
    Restituisce stringa vuota se vat è vuoto/None.
    """
    if not vat:
        return ''
    vat = vat.strip().upper()
    # Se i primi due caratteri sono già due lettere il prefisso paese è già presente
    if len(vat) >= 2 and vat[0].isalpha() and vat[1].isalpha():
        return vat
    if not country_id_val:
        return vat
    try:
        country = env['res.country'].sudo().browse(int(country_id_val))
        if country.exists() and country.code == 'IT':
            vat = 'IT' + vat
    except (ValueError, TypeError):
        pass
    return vat


class BuildingPaySignup(AuthSignupHome):
    """
    Estende il controller di registrazione standard di Odoo per:
    1. Mostrare campi aggiuntivi (nome, indirizzo, CF/P.IVA, IBAN, banca)
    2. Catturare il codice referrer dall'URL
    3. Dopo la registrazione, configurare il partner come Amministratore
       e collegare il referrer
    4. Creare il record res.partner.bank con l'IBAN inserito
    5. Inviare l'email di benvenuto
    """

    @http.route('/web/signup', type='http', auth='public', website=True, sitemap=False)
    def web_auth_signup(self, *args, **kw):
        """
        Override del form di registrazione.
        Il link BuildingPay ha la forma: /web/signup?referrer=CODICE123
        """
        # ------------------------------------------------------------------
        # BYPASS 1: inviti via token (es. "Concedi accesso portale" dal backend)
        # Quando un admin invita un utente, Odoo manda /web/signup?token=XXXX.
        # In quel caso usa il flusso standard Odoo (solo scelta password).
        # ------------------------------------------------------------------
        token = kw.get('token') or request.params.get('token', '')
        if token:
            return super().web_auth_signup(*args, **kw)

        # Legge il referrer_code SOLO dai parametri URL/form — MAI dalla sessione
        # per il rendering del template.
        # ◆ In GET:  arriva come ?referrer=CODICE nell'URL
        # ◆ In POST: arriva dal campo hidden <input name="referrer_code">
        # Non si usa la sessione per il rendering perché vecchi codici in sessione
        # nasconderebbero il blocco "registrazione non autorizzata" quando l'URL
        # non contiene il parametro referrer.
        referrer_code = (
            kw.get('referrer')
            or request.params.get('referrer', '')
            or kw.get('referrer_code')
            or request.params.get('referrer_code', '')
        ).strip()

        if referrer_code:
            # Salva in sessione solo se presente (per il POST dopo il GET)
            request.session['buildingpay_referrer_code'] = referrer_code
        else:
            # Nessun codice nell'URL: pulisce la sessione per evitare che
            # un vecchio codice salvo in sessione nasconda il blocco di errore.
            request.session.pop('buildingpay_referrer_code', None)

        qcontext = self.get_auth_signup_qcontext()
        qcontext['referrer_code'] = referrer_code  # Solo dal URL, non dalla sessione
        qcontext['banks'] = request.env['res.bank'].sudo().search([], order='name')

        # ------------------------------------------------------------------
        # BYPASS 2: nessuna configurazione BuildingPay per questo sito web
        # ------------------------------------------------------------------
        config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()
        if not config:
            return super().web_auth_signup(*args, **kw)

        # Cerca il referrer SENZA filtro is_amministratore:
        # il referrer può essere qualsiasi contatto con un codice referrer,
        # non necessariamente un amministratore BuildingPay.
        # Controllo lunghezza: il codice referrer deve essere esattamente 8 caratteri.
        if referrer_code:
            if len(referrer_code) != 8:
                qcontext['referrer_partner'] = False
                qcontext['referrer_code_invalid'] = True
                qcontext['referrer_code_wrong_length'] = True
            else:
                referrer = request.env['res.partner'].sudo().search([
                    ('referrer_code', '=', referrer_code),
                ], limit=1)
                qcontext['referrer_partner'] = referrer
                qcontext['referrer_code_invalid'] = not bool(referrer)
                qcontext['referrer_code_wrong_length'] = False
        else:
            qcontext['referrer_partner'] = False
            qcontext['referrer_code_invalid'] = False
            qcontext['referrer_code_wrong_length'] = False

        if request.httprequest.method == 'GET':
            return request.render('BuildingPay_onboarding_v15.signup_form', qcontext)

        # POST: elabora il form BuildingPay
        return self._process_buildingpay_signup(qcontext, **kw)

    def _process_buildingpay_signup(self, qcontext, **kw):
        """
        Elabora il form di registrazione BuildingPay.

        Strategia:
        1. Valida i campi obbligatori
        2. Cattura referrer_code PRIMA di do_signup() (la sessione viene
           rigenerata da session.authenticate() dentro do_signup)
        3. Chiama self.do_signup(qcontext) – il metodo ufficiale Odoo che:
           - crea l'utente tramite res.users.signup()
           - fa request.env.cr.commit()
           - autentica la sessione via request.session.authenticate()
        4. Dopo do_signup(), usa request.session.uid per trovare il partner
           appena creato in modo affidabile
        5. Aggiorna il partner con tutti i dati aggiuntivi
        6. Crea il record IBAN in res.partner.bank
        """
        params = request.params

        # ------------------------------------------------------------------
        # Blocco POST senza referrer code
        # (doppio controllo server-side: il form è nascosto lato template,
        #  ma previene invii diretti via POST senza codice referrer)
        # ------------------------------------------------------------------
        referrer_code_post = (
            params.get('referrer_code', '').strip()
            or qcontext.get('referrer_code', '')
            or request.session.get('buildingpay_referrer_code', '')
        )
        # Il codice referrer è facoltativo: se assente la registrazione prosegue
        # senza collegare un referrer (il LEAD e il contatto non avranno referrer_id).

        # ------------------------------------------------------------------
        # Intercept intent=info: crea un lead CRM invece di registrare
        # ------------------------------------------------------------------
        if params.get('intent') == 'info':
            return self._handle_info_request(params, referrer_code_post, qcontext)

        # ------------------------------------------------------------------
        # Validazione campi obbligatori
        # ------------------------------------------------------------------
        errors = {}

        required_fields = ['name', 'phone', 'pec_mail', 'login',
                           'password', 'confirm_password',
                           'street', 'city', 'zip', 'sistema_pagamento']
        for field in required_fields:
            if not params.get(field, '').strip():
                errors[field] = _('Campo obbligatorio')

        if params.get('password') != params.get('confirm_password'):
            errors['confirm_password'] = _('Le password non coincidono')

        # Validazione robustezza password
        pwd = params.get('password', '')
        if pwd and 'password' not in errors:
            if len(pwd) < 8:
                errors['password'] = _('La password deve avere almeno 8 caratteri.')
            elif not re.search(r'[a-z]', pwd):
                errors['password'] = _('La password deve contenere almeno una lettera minuscola.')
            elif not re.search(r'[A-Z]', pwd):
                errors['password'] = _('La password deve contenere almeno una lettera maiuscola.')
            elif not re.search(r'[^a-zA-Z0-9]', pwd):
                errors['password'] = _('La password deve contenere almeno un carattere speciale (es. ! @ # $ % &).')

        # Validazione IBAN: usa validate_iban() nativo Odoo (base_iban).
        # validate_iban() solleva ValidationError se l'IBAN è formalmente errato.
        iban = params.get('iban', '').replace(' ', '').upper().strip()
        if iban:
            try:
                validate_iban(iban)
            except ValidationError as e:
                # Propaghiamo il messaggio specifico di Odoo (tradotto in italiano
                # se la lingua it_IT è installata), es:
                #   "L'IBAN non sembra corretto. Avresti dovuto inserire qualcosa come IT..."
                #   "Questo IBAN non supera il controllo di validazione, si prega di verificarlo."
                #   "L'IBAN non è valido, dovrebbe iniziare con il codice paese"
                msg = e.args[0] if e.args else _('IBAN non valido — verificare il codice inserito')
                errors['iban'] = msg

        # Controllo duplicato codice fiscale
        # Se il CF esiste già nei contatti Odoo, blocca la registrazione.
        fiscalcode_input = params.get('fiscalcode', '').strip().upper()
        if fiscalcode_input:
            existing_cf = request.env['res.partner'].sudo().search([
                ('fiscalcode', '=ilike', fiscalcode_input),
            ], limit=1)
            if existing_cf:
                errors['fiscalcode'] = _(
                    'Il Codice Fiscale "%s" è già registrato nel sistema. '
                    'Se hai già un account, accedi con le tue credenziali esistenti. '
                    'Per assistenza contatta il supporto BuildingPay.'
                ) % fiscalcode_input

        # Lunghezza massima sistema_pagamento
        sistema_pagamento = params.get('sistema_pagamento', '').strip()
        if sistema_pagamento and len(sistema_pagamento) > 10:
            errors['sistema_pagamento'] = _('Massimo 10 caratteri')

        # Accettazione privacy obbligatoria
        if not params.get('privacy_accepted'):
            errors['privacy_accepted'] = _('Devi accettare la Privacy Policy per registrarti')

        if errors:
            qcontext.update({'error': errors, 'form_data': params})
            # Ri-popola le banche nel select in caso di errore (potrebbero non essere in qcontext)
            if 'banks' not in qcontext:
                qcontext['banks'] = request.env['res.bank'].sudo().search([], order='name')
            return request.render('BuildingPay_onboarding_v15.signup_form', qcontext)

        login = params.get('login', '').strip()
        password = params.get('password', '')
        name = params.get('name', '').strip()

        # ------------------------------------------------------------------
        # IMPORTANTE: cattura tutti i valori necessari PRIMA di do_signup().
        # do_signup() chiama request.session.authenticate() che rigenera la
        # sessione, perdendo i valori salvati precedentemente
        # (es. buildingpay_referrer_code).
        # Salviamo quindi in variabili locali prima del signup.
        # ------------------------------------------------------------------
        referrer_code_local = (
            params.get('referrer_code', '').strip()
            or qcontext.get('referrer_code', '')
            or request.session.get('buildingpay_referrer_code', '')
        )
        privacy_accepted = bool(params.get('privacy_accepted'))

        # banca_id: ID numerico del record res.bank selezionato nel form.
        # Catturato prima di do_signup() perché la sessione viene rigenerata.
        banca_id_local = 0
        try:
            raw = params.get('banca_id', '') or ''
            banca_id_local = int(raw) if raw.strip() else 0
        except (ValueError, TypeError):
            banca_id_local = 0

        # ------------------------------------------------------------------
        # Step 1: Creazione account tramite do_signup() ufficiale di Odoo.
        # do_signup() chiama internamente:
        #   res.users.signup(values)  →  crea l'utente
        #   request.env.cr.commit()   →  commit della transazione
        #   request.session.authenticate(db, login, pw)  →  autentica la sessione
        # ------------------------------------------------------------------
        qcontext['login'] = login
        qcontext['password'] = password
        qcontext['name'] = name

        try:
            self.do_signup(qcontext)
        except Exception as e:
            _logger.error('BuildingPay signup – errore creazione utente: %s', e)
            qcontext['error'] = {
                'general': _('Errore durante la creazione dell\'account: %s') % str(e)
            }
            return request.render('BuildingPay_onboarding_v15.signup_form', qcontext)

        # ------------------------------------------------------------------
        # Step 2: Recupera il partner dal session.uid (affidabile dopo do_signup)
        # ------------------------------------------------------------------
        try:
            uid = request.session.uid
            if uid:
                partner = request.env['res.users'].sudo().browse(uid).partner_id
            else:
                # Fallback nel caso authenticate() non abbia aggiornato la sessione
                user = request.env['res.users'].sudo().search(
                    [('login', '=', login)], limit=1)
                partner = user.partner_id if user else None

            if not partner:
                _logger.error('BuildingPay signup – partner non trovato per uid=%s login=%s',
                              uid, login)
                return request.redirect('/my/home')

            # --------------------------------------------------------------
            # Step 3a: Imposta is_amministratore = True e
            #          is_amministratore_validato = False (write ISOLATO).
            # Il flag validato è FALSE di default: l'operatore lo imposta
            # a TRUE manualmente dopo aver controllato i dati.
            # CRITICO: write isolato → se un campo non esiste, l'eccezione
            # non blocca i passi successivi (IBAN, referrer, ecc.).
            # --------------------------------------------------------------
            partner.sudo().write({
                'is_amministratore': True,
                'is_amministratore_validato': False,
            })

            # company_type: 'person' o 'company' dal radio button del form
            company_type = params.get('company_type', '').strip()
            if company_type in ('person', 'company'):
                try:
                    partner.sudo().write({'company_type': company_type})
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore salvataggio company_type: %s', e)

            # --------------------------------------------------------------
            # Step 3b: Privacy accepted (write separato con proprio try/except)
            # --------------------------------------------------------------
            if privacy_accepted:
                try:
                    partner.sudo().write({
                        'privacy_accepted': True,
                        'privacy_accepted_date': odoo_fields.Datetime.now(),
                    })
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore salvataggio privacy: %s', e)

            # --------------------------------------------------------------
            # Step 3c: Lingua di default = italiano (it_IT)
            # --------------------------------------------------------------
            try:
                lang = request.env['res.lang'].sudo().search(
                    [('code', '=', 'it_IT'), ('active', '=', True)], limit=1)
                if lang:
                    partner.sudo().write({'lang': 'it_IT'})
                else:
                    _logger.warning('BuildingPay signup – lingua it_IT non attiva in questo Odoo')
            except Exception as e:
                _logger.warning('BuildingPay signup – errore impostazione lingua: %s', e)

            # --------------------------------------------------------------
            # Step 4: Dati anagrafici (indirizzo, telefono)
            # Separati da fiscalcode: l10n_it_edi potrebbe applicare una
            # constraint sul CF (formato 16 char alfanumerico). Se il CF
            # non passa la validazione, l'errore non deve bloccare il
            # salvataggio dell'indirizzo e degli step successivi.
            # --------------------------------------------------------------
            anagrafici = {}
            for field in ['street', 'street2', 'city', 'zip', 'phone']:
                val = params.get(field, '').strip()
                if val:
                    anagrafici[field] = val

            if anagrafici:
                try:
                    partner.sudo().write(anagrafici)
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore salvataggio anagrafici: %s', e)

            # Codice fiscale in savepoint isolato: se la validazione Odoo
            # fallisce, il savepoint viene rilasciato senza invalidare la
            # transazione principale (indirizzo, IBAN, referrer restano).
            if params.get('fiscalcode', '').strip():
                try:
                    with request.env.cr.savepoint():
                        partner.sudo().write(
                            {'fiscalcode': params['fiscalcode'].strip()})
                except Exception as e:
                    _logger.warning(
                        'BuildingPay signup – CF non salvato (%s): %s',
                        params.get('fiscalcode'), e)

            # --------------------------------------------------------------
            # Step 4b: Note → comment
            # --------------------------------------------------------------
            note_val = params.get('note', '').strip()
            if note_val:
                try:
                    partner.sudo().write({'comment': note_val})
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore salvataggio note: %s', e)

            # --------------------------------------------------------------
            # Step 4c: Campi aggiuntivi BuildingPay
            #   - pec_mail: email PEC (obbligatoria)
            #   - nome_promotore: promotore che ha presentato l'amministratore
            #   - applicativo: software gestionale in uso
            #   - sistema_pagamento: modalità di pagamento rate (max 10 car.)
            # --------------------------------------------------------------
            extra_fields = {}
            for field in ['pec_mail', 'applicativo', 'sistema_pagamento']:
                val = params.get(field, '').strip()
                if val:
                    extra_fields[field] = val
            if extra_fields:
                try:
                    partner.sudo().write(extra_fields)
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore salvataggio campi extra: %s', e)

            # --------------------------------------------------------------
            # Step 4d: website_id — associa il partner al sito BuildingPay
            # Cerca il sito web che si chiama "BuildingPay" (case-insensitive)
            # --------------------------------------------------------------
            try:
                bp_website = request.env['website'].sudo().search([
                    ('name', 'ilike', 'BuildingPay'),
                ], limit=1)
                if bp_website:
                    partner.sudo().write({'website_id': bp_website.id})
                else:
                    _logger.warning(
                        'BuildingPay signup: sito web "BuildingPay" non trovato '
                        '— website_id non impostato.')
            except Exception as e:
                _logger.warning('BuildingPay signup – errore salvataggio website_id: %s', e)

            # --------------------------------------------------------------
            # Step 5: Codice fiscale / P.IVA con eventuale validazione Odoo
            # Separato da anagrafici per evitare che un errore di validazione
            # impedisca di salvare i dati anagrafici
            # --------------------------------------------------------------
            if params.get('vat', '').strip():
                vat_normalized = _normalize_vat_prefix(
                    request.env, params['vat'].strip(), params.get('country_id'))
                try:
                    partner.sudo().write({'vat': vat_normalized})
                except Exception as e:
                    _logger.warning('BuildingPay signup – P.IVA non valida (%s): %s',
                                    vat_normalized, e)

            # --------------------------------------------------------------
            # Step 6: Paese e provincia
            # country_id è un select con l'ID numerico del paese.
            # state_code è un campo testo con la sigla provincia (es. RM).
            # Cerchiamo lo state_id corrispondente in res.country.state.
            # --------------------------------------------------------------
            try:
                geo = {}
                raw_country = params.get('country_id', '') or ''
                if raw_country:
                    cid = int(raw_country)
                    if cid:
                        geo['country_id'] = cid

                state_code = params.get('state_code', '').strip().upper()
                if state_code:
                    state_domain = [('code', '=', state_code)]
                    if geo.get('country_id'):
                        state_domain.append(('country_id', '=', geo['country_id']))
                    state = request.env['res.country.state'].sudo().search(
                        state_domain, limit=1)
                    if state:
                        geo['state_id'] = state.id

                if geo:
                    partner.sudo().write(geo)
            except (ValueError, TypeError) as e:
                _logger.warning('BuildingPay signup – country/state non valido: %s', e)

            # --------------------------------------------------------------
            # Step 7: Referrer
            # Usa referrer_code_local (catturato PRIMA di do_signup) per
            # evitare perdita del valore dopo la rigenerazione della sessione.
            # NOTA: la ricerca usa solo referrer_code (non is_amministratore)
            # perché il referrer potrebbe essere un partner senza quel flag.
            # --------------------------------------------------------------
            if referrer_code_local:
                try:
                    _logger.info('BuildingPay signup: cerco referrer con codice "%s"',
                                 referrer_code_local)
                    referrer = request.env['res.partner'].sudo().search([
                        ('referrer_code', '=', referrer_code_local),
                    ], limit=1)
                    if referrer:
                        partner.sudo().write({'referrer_id': referrer.id})
                        _logger.info('BuildingPay signup: referrer_id=%s (%s) collegato a partner %s',
                                     referrer.id, referrer.name, partner.id)
                    else:
                        _logger.warning('BuildingPay signup: nessun partner con referrer_code="%s"',
                                        referrer_code_local)
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore collegamento referrer: %s', e)

            # --------------------------------------------------------------
            # Step 8: IBAN → res.partner.bank (modello nativo Odoo)
            # banca_id_local è l'ID del record res.bank selezionato dal form.
            # FIX CRITICO: avvolto in savepoint perché res.partner.bank ha un
            # constraint di unicità su acc_number. Senza savepoint, se create()
            # solleva un'eccezione a livello PostgreSQL (es. IBAN duplicato),
            # la transazione entra in stato ABORTED e TUTTI i write precedenti
            # (is_amministratore, indirizzo, telefono, ecc.) vengono persi al
            # commit finale. Il savepoint isola il fallimento al solo IBAN.
            # --------------------------------------------------------------
            if iban:
                try:
                    with request.env.cr.savepoint():
                        bank_vals = {
                            'partner_id': partner.id,
                            'acc_number': iban,
                            'company_id': (partner.company_id.id
                                           if partner.company_id else False),
                        }
                        if banca_id_local:
                            bank_vals['bank_id'] = banca_id_local
                            _logger.info('BuildingPay signup: banca id=%s collegata', banca_id_local)
                        request.env['res.partner.bank'].sudo().create(bank_vals)
                        _logger.info('BuildingPay signup: IBAN salvato per partner %s', partner.id)
                except Exception as e:
                    _logger.warning('BuildingPay signup – errore creazione IBAN: %s', e)

            # Pulisci sessione (potrebbe essere ancora presente se non rigenerata)
            request.session.pop('buildingpay_referrer_code', None)

            # Email di benvenuto
            self._send_welcome_email(partner)

            # Attività di controllo dati nuovo amministratore
            self._create_new_admin_activity(partner)

            # Lead CRM per tracciare la registrazione (stesso flusso del form informazioni)
            try:
                self._create_buildingpay_lead(
                    name=partner.name or params.get('name', '').strip(),
                    email=login,
                    phone=params.get('phone', '').strip(),
                    referrer_code=referrer_code_local,
                    partner=partner,
                    is_registration=True,
                )
            except Exception as e:
                _logger.warning('BuildingPay signup – errore creazione lead CRM: %s', e)

            _logger.info('BuildingPay: nuovo amministratore registrato: %s (%s)',
                         partner.name, partner.email)

        except Exception as e:
            _logger.error('BuildingPay signup – errore post-creazione: %s', e)
            # L'account è già stato creato correttamente.
            # Non mostriamo un errore bloccante ma logghiamo per debug.

        return request.redirect('/my/home')

    def _send_welcome_email(self, partner):
        """
        Invia l'email di benvenuto al nuovo amministratore e la registra nel chatter.

        NOTA: NON usiamo l'XML ID (module.name) per trovare il template perché
        il nome del modulo cambia ad ogni versione (BuildingPay_v71, _v72, …).
        In caso di aggiornamento, ir.model.data conserva il nome della versione
        che ha CREATO il record originariamente: una ricerca su module='BuildingPay_onboarding_v15'
        non troverebbe nulla se il template era stato installato da una versione
        precedente, l'eccezione verrebbe silenziata e la mail non partirebbe.
        Soluzione: cercare mail.template direttamente per nome e modello (invarianti
        tra versioni) tramite sudo().
        """
        try:
            template = request.env['mail.template'].sudo().search([
                ('name', '=', 'Mail benvenuto amministratore'),
                ('model', '=', 'res.partner'),
            ], limit=1)
            if not template:
                _logger.warning(
                    'BuildingPay: template email benvenuto non trovato '
                    '(cercare "Mail benvenuto amministratore" in Impostazioni → Email → Template)')
                return

            # send_mail crea mail.message (model=res.partner, res_id=partner.id)
            # + mail.mail collegato → l'email compare nel chatter del contatto.
            template.send_mail(partner.id, force_send=True)
            _logger.info(
                'BuildingPay: email benvenuto inviata a %s (%s)',
                partner.name, partner.email)
        except Exception as e:
            _logger.error('BuildingPay: errore invio email benvenuto: %s', e)

    def _create_new_admin_activity(self, partner):
        """
        Crea un'attività "To Do" per ogni assegnatario configurato (fino a 4).
        Ogni assegnatario riceve un ToDo separato con stesso titolo, stessa
        scadenza e stesso testo descrittivo.

        L'attività viene creata solo se:
        - la configurazione BuildingPay ha create_activity_on_new_admin = True
        - è configurato almeno il primo assegnatario
        """
        from datetime import date, timedelta
        try:
            config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()
            if not config or not config.create_activity_on_new_admin:
                return

            # Raccoglie tutti gli assegnatari configurati (fino a 4)
            responsabili = [
                config.activity_new_admin_responsible_1_id,
                config.activity_new_admin_responsible_2_id,
                config.activity_new_admin_responsible_3_id,
                config.activity_new_admin_responsible_4_id,
            ]
            responsabili = [r for r in responsabili if r]  # filtra i campi vuoti

            if not responsabili:
                _logger.warning(
                    'BuildingPay: attività nuovo admin non creata: '
                    'nessun assegnatario configurato')
                return

            days = config.activity_new_admin_days or 5
            deadline = date.today() + timedelta(days=days)

            activity_type = request.env.ref(
                'mail.mail_activity_data_todo', raise_if_not_found=False)

            summary = _('Nuovo amministratore da validare: %s') % partner.name
            note = _(
                'Nuovo amministratore registrato. '
                'Controllare i dati inseriti ed effettuare una ricerca '
                "sull\u2019onorabilit\u00e0 dell\u2019amministratore."
            )

            # Crea un ToDo separato per ogni assegnatario
            for user in responsabili:
                partner.sudo().activity_schedule(
                    activity_type_id=activity_type.id if activity_type else False,
                    summary=summary,
                    note=note,
                    date_deadline=deadline,
                    user_id=user.id,
                )
                _logger.info(
                    'BuildingPay: attività "Nuovo amministratore" creata per partner %s '
                    '(assegnatario: %s, scadenza: %s)',
                    partner.id, user.name, deadline,
                )
        except Exception as e:
            _logger.warning(
                'BuildingPay: errore creazione attività nuovo admin: %s', e)

    def _handle_info_request(self, params, referrer_code, qcontext):
        """
        Gestisce la richiesta di informazioni (intent=info).
        Valida nome e email, crea un lead CRM e reindirizza con info_sent=1.
        """
        name = params.get('info_name', '').strip()
        email = params.get('info_email', '').strip()
        phone = params.get('info_phone', '').strip()

        errors = {}
        if not name:
            errors['info_name'] = _('Campo obbligatorio')
        if not phone:
            errors['info_phone'] = _('Campo obbligatorio')
        if not email:
            errors['info_email'] = _('Campo obbligatorio')

        if errors:
            qcontext.update({
                'error': errors,
                'form_data': params,
                'intent_info_active': True,
            })
            if 'banks' not in qcontext:
                qcontext['banks'] = request.env['res.bank'].sudo().search([], order='name')
            return request.render('BuildingPay_onboarding_v15.signup_form', qcontext)

        try:
            self._create_buildingpay_lead(name, email, phone, referrer_code)
        except Exception as e:
            _logger.error('BuildingPay info request – errore creazione lead: %s', e)

        if referrer_code:
            redirect_url = '/web/signup?referrer=%s&info_sent=1' % referrer_code
        else:
            redirect_url = '/web/signup?info_sent=1'
        return request.redirect(redirect_url)

    def _create_buildingpay_lead(self, name, email, phone, referrer_code, partner=None, is_registration=False):
        """Crea un lead CRM BuildingPay. Se partner è fornito, lo collega al lead."""
        env = request.env

        # Trova o crea il tag CRM "BuildingPay"
        tag = env['crm.tag'].sudo().search([('name', '=', 'BuildingPay')], limit=1)
        if not tag:
            tag = env['crm.tag'].sudo().create({'name': 'BuildingPay'})

        # Determina il salesperson in base al flag use_referrer_salesperson del referrer:
        # - TRUE: usa user_id del referrer se impostato, altrimenti default config
        # - FALSE: usa sempre il default dalla config BuildingPay
        user_id = False
        referrer_partner = False
        if referrer_code:
            referrer_partner = env['res.partner'].sudo().search([
                ('referrer_code', '=', referrer_code),
            ], limit=1)

        config = env['buildingpay_v36.config'].sudo().get_config_for_website()

        if referrer_partner and referrer_partner.use_referrer_salesperson:
            if referrer_partner.user_id:
                user_id = referrer_partner.user_id.id
            elif config and config.default_salesperson_id:
                user_id = config.default_salesperson_id.id
        else:
            if config and config.default_salesperson_id:
                user_id = config.default_salesperson_id.id

        lead_name = ('BuildingPay - amministratore registrato'
                     if is_registration else 'BuildingPay - Richiesta informazioni')
        lead_vals = {
            'name': lead_name,
            'contact_name': name,
            'email_from': email,
            'tag_ids': [(4, tag.id)],
            'type': 'lead',
            'is_amministratore_buildingpay': is_registration,
        }
        if phone:
            lead_vals['phone'] = phone
        if user_id:
            lead_vals['user_id'] = user_id
        if referrer_partner:
            lead_vals['referrer_id'] = referrer_partner.id
        if partner:
            lead_vals['partner_id'] = partner.id

        lead = env['crm.lead'].sudo().create(lead_vals)
        _logger.info(
            'BuildingPay: lead CRM creato (id=%s) per richiesta informazioni da %s',
            lead.id, email)

        # Crea un'attività To Do per ogni assegnatario configurato (fino a 3)
        self._create_info_lead_activities(lead, name, email, phone)

        return lead

    def _create_info_lead_activities(self, lead, name, email, phone):
        """
        Crea un'attività To Do su crm.lead per ogni assegnatario configurato.
        Scadenza: oggi + activity_info_lead_days (default 2).
        """
        from datetime import date, timedelta
        try:
            config = request.env['buildingpay_v36.config'].sudo().get_config_for_website()
            if not config:
                return

            responsabili = [
                config.activity_info_lead_responsible_1_id,
                config.activity_info_lead_responsible_2_id,
                config.activity_info_lead_responsible_3_id,
            ]
            responsabili = [r for r in responsabili if r]
            if not responsabili:
                return

            days = config.activity_info_lead_days if config.activity_info_lead_days and config.activity_info_lead_days > 0 else 2
            deadline = date.today() + timedelta(days=days)

            activity_type = request.env.ref(
                'mail.mail_activity_data_todo', raise_if_not_found=False)

            summary = _('Lead BuildingPay - richiesta Informazioni')
            phone_part = ', telefono %s' % phone if phone else ''
            note = _(
                'Contattare %s, email %s%s per fornire informazioni su BuildingPay.'
            ) % (name, email, phone_part)

            # Aggiunge link di iscrizione SOLO per richieste informazioni (non per registrazioni)
            if not lead.is_amministratore_buildingpay:
                try:
                    bp_website = request.env['website'].sudo().search([
                        ('name', 'ilike', 'BuildingPay'),
                    ], limit=1)
                    if bp_website and bp_website.domain:
                        domain = bp_website.domain.strip().rstrip('/')
                        if not domain.startswith('http'):
                            domain = 'https://' + domain
                    else:
                        domain = request.env['ir.config_parameter'].sudo().get_param(
                            'web.base.url', '').rstrip('/')
                    signup_url = domain + '/web/signup'
                    if lead.referrer_id and lead.referrer_id.referrer_code:
                        signup_url += '?referrer=%s' % lead.referrer_id.referrer_code
                    note += (
                        '\nDopo aver effettuato le proprie attività di sales, '
                        'invitare l\'amministratore a registrarsi a BuildingPay '
                        'tramite il link: %s %s'
                    ) % (domain, signup_url)
                except Exception as e_link:
                    _logger.warning(
                        'BuildingPay: errore generazione link signup per attività: %s', e_link)

            for user in responsabili:
                lead.sudo().activity_schedule(
                    activity_type_id=activity_type.id if activity_type else False,
                    summary=summary,
                    note=note,
                    date_deadline=deadline,
                    user_id=user.id,
                )
                _logger.info(
                    'BuildingPay: attività lead informazioni creata su lead %s '
                    '(assegnatario: %s, scadenza: %s)',
                    lead.id, user.name, deadline,
                )
        except Exception as e:
            _logger.warning(
                'BuildingPay: errore creazione attività lead informazioni: %s', e)

    def _get_referral_url(self, partner):
        """Genera il link referral per un amministratore."""
        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')
        if partner.referrer_code:
            return '%s/web/signup?referrer=%s' % (base_url, partner.referrer_code)
        return base_url
