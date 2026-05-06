/* global fetch */
'use strict';

/**
 * BuildingPay IBAN Validator
 *
 * Validazione IBAN client-side (MOD97) + lookup automatico banca via
 * l'endpoint JSON-RPC /buildingpay/validate_iban (schwifty sul backend).
 *
 * Utilizzo — da chiamare una volta quando il DOM è pronto:
 *   BuildingPayIban.init(ibanInputId, bancaHiddenId, bancaDisplayId, feedbackId);
 */
(function () {

    // ------------------------------------------------------------------ //
    // MOD97 — validazione checksum IBAN pura JS (nessuna dipendenza esterna)
    // ------------------------------------------------------------------ //
    function mod97(iban) {
        var clean = iban.replace(/\s+/g, '').toUpperCase();
        if (clean.length < 4) { return false; }
        var rearranged = clean.slice(4) + clean.slice(0, 4);
        var numeric = '';
        for (var i = 0; i < rearranged.length; i++) {
            var c = rearranged[i];
            if (c >= '0' && c <= '9') {
                numeric += c;
            } else if (c >= 'A' && c <= 'Z') {
                numeric += (c.charCodeAt(0) - 55).toString();
            } else {
                return false;
            }
        }
        var rem = 0;
        for (var j = 0; j < numeric.length; j++) {
            rem = (rem * 10 + parseInt(numeric[j], 10)) % 97;
        }
        return rem === 1;
    }

    // ------------------------------------------------------------------ //
    // Core initialiser
    // ------------------------------------------------------------------ //
    function init(ibanInputId, bancaHiddenId, bancaDisplayId, feedbackId) {
        var ibanEl    = document.getElementById(ibanInputId);
        var hiddenEl  = document.getElementById(bancaHiddenId);
        var displayEl = document.getElementById(bancaDisplayId);
        var fbEl      = document.getElementById(feedbackId);

        if (!ibanEl) { return; }

        var debounceTimer = null;

        // ---- helpers --------------------------------------------------

        function clearBank() {
            if (hiddenEl)  { hiddenEl.value  = ''; }
            if (displayEl) { displayEl.value = ''; }
        }

        function setFeedback(type, msg) {
            if (!fbEl) { return; }
            fbEl.className = 'bp-iban-feedback form-text mt-1';
            // Rende sempre visibile il div feedback
            fbEl.style.display = 'block';
            switch (type) {
                case 'error':
                    fbEl.className += ' text-danger';
                    fbEl.innerHTML = '<i class="fa fa-times-circle me-1"></i>' + msg;
                    ibanEl.classList.add('is-invalid');
                    ibanEl.classList.remove('is-valid');
                    break;
                case 'success':
                    fbEl.className += ' text-success';
                    fbEl.innerHTML = '<i class="fa fa-check-circle me-1"></i>' + msg;
                    ibanEl.classList.remove('is-invalid');
                    ibanEl.classList.add('is-valid');
                    break;
                case 'loading':
                    fbEl.className += ' text-muted';
                    fbEl.innerHTML = '<i class="fa fa-spinner fa-spin me-1"></i>' + msg;
                    ibanEl.classList.remove('is-invalid');
                    ibanEl.classList.remove('is-valid');
                    break;
                default:
                    fbEl.innerHTML = '';
                    fbEl.style.display = 'none';
                    ibanEl.classList.remove('is-invalid');
                    ibanEl.classList.remove('is-valid');
            }
        }

        // ---- main validation ------------------------------------------

        function validate(rawIban) {
            var clean = rawIban.replace(/\s+/g, '').toUpperCase();

            if (!clean) {
                setFeedback('none', '');
                clearBank();
                return;
            }

            // Controllo MOD97 lato client (immediato, senza chiamata al server)
            if (!mod97(clean)) {
                setFeedback('error', 'IBAN non valido — verificare il codice inserito');
                clearBank();
                return;
            }

            // MOD97 ok → chiamo il backend per il lookup banca (schwifty)
            setFeedback('loading', 'Ricerca istituto bancario…');

            var csrfEl = document.querySelector('input[name="csrf_token"]');
            var headers = { 'Content-Type': 'application/json' };
            if (csrfEl) { headers['X-CSRFToken'] = csrfEl.value; }

            fetch('/buildingpay/validate_iban', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({
                    jsonrpc: '2.0', method: 'call', id: 1,
                    params: { iban: clean }
                })
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var res = (data || {}).result;
                if (!res) {
                    // Risposta malformata — l'IBAN era già validato lato client
                    setFeedback('success', 'IBAN valido — impossibile contattare il server per la banca');
                    clearBank();
                    return;
                }
                if (!res.valid) {
                    setFeedback('error', res.error || 'IBAN non valido');
                    clearBank();
                    return;
                }

                // IBAN valido: aggiorno campo banca
                if (res.bank_id) {
                    // Banca trovata nel registro Odoo (res.bank)
                    if (hiddenEl)  { hiddenEl.value  = res.bank_id; }
                    if (displayEl) { displayEl.value = res.bank_name; }
                    setFeedback('success',
                        'IBAN valido &nbsp;·&nbsp; <strong>' + res.bank_name + '</strong>');
                } else if (res.bank_name) {
                    // Banca rilevata da schwifty ma non ancora in Odoo (creazione fallita)
                    if (hiddenEl)  { hiddenEl.value  = ''; }
                    if (displayEl) { displayEl.value = res.bank_name; }
                    setFeedback('success',
                        'IBAN valido &nbsp;·&nbsp; ' + res.bank_name +
                        ' <small class="text-muted">(banca non ancora in archivio)</small>');
                } else {
                    // IBAN valido ma banca non rilevata (fuori dal registro schwifty)
                    clearBank();
                    setFeedback('success',
                        'IBAN valido &nbsp;·&nbsp; ' +
                        '<span class="text-warning">' +
                        '<i class="fa fa-exclamation-triangle me-1"></i>' +
                        'istituto bancario non rilevato automaticamente</span>');
                }
            })
            .catch(function () {
                // Errore di rete: IBAN era già valido lato client, lo comunico
                setFeedback('success',
                    'IBAN valido &nbsp;·&nbsp; ' +
                    '<span class="text-muted">banca non verificata (errore di rete)</span>');
            });
        }

        // ---- event listeners ------------------------------------------

        ibanEl.addEventListener('input', function () {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () { validate(ibanEl.value); }, 600);
        });

        ibanEl.addEventListener('blur', function () {
            clearTimeout(debounceTimer);
            validate(ibanEl.value);
        });

        // Auto-validate se il campo ha già un valore al caricamento (modalità modifica)
        if (ibanEl.value && ibanEl.value.trim()) {
            validate(ibanEl.value);
        }

        // Blocca il submit se l'IBAN è presente ma non supera il MOD97
        var form = ibanEl.closest('form');
        if (form) {
            form.addEventListener('submit', function (e) {
                var clean = ibanEl.value.replace(/\s+/g, '').toUpperCase();
                if (clean && !mod97(clean)) {
                    e.preventDefault();
                    setFeedback('error', 'IBAN non valido — correggere prima di inviare');
                    ibanEl.focus();
                }
            });
        }
    }

    // ------------------------------------------------------------------ //
    // Registrazione: inizializza quando richiesto dai template inline
    // Usa il pattern "readyState" per funzionare sia se il DOM è già pronto
    // (script nel body a fine pagina) sia se non lo è ancora (script in head).
    // ------------------------------------------------------------------ //
    window.BuildingPayIban = {
        init: function (ibanId, hiddenId, displayId, fbId) {
            function doInit() { init(ibanId, hiddenId, displayId, fbId); }
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', doInit);
            } else {
                // DOM già pronto (es. script inline nel body a fine pagina)
                doInit();
            }
        }
    };

}());
