# BuildingPay — Diagramma di flusso logica applicativa
> Versione modulo: 17.0.6.0.8  
> Entry point: `/web/signup`

---

## 1. Flusso Signup

```mermaid
flowchart TD
    START(["/web/signup"]) --> RC{referrer_code presente nell URL?}

    RC -->|No| ERR1["Render form Errore\nRegistrazione disponibile solo\ntramite link con codice referrer\n— EXIT —"]
    RC -->|Si| LEN{lunghezza = 8 caratteri?}

    LEN -->|No| ERR2["Render form Errore\nCodice referrer non valido\nlunghezza errata\n— EXIT —"]
    LEN -->|Si| DB{Partner con referrer_code\ntrovato nel DB?}

    DB -->|Non trovato| ERR3["Render form Errore\nCodice referrer non riconosciuto\n— EXIT —"]
    DB -->|Trovato| METHOD{GET o POST?}

    METHOD -->|GET| SHOW["Mostra form registrazione\ncon nome referrer visibile"]
    METHOD -->|POST| INTENT{"Tipo richiesta?"}

    INTENT -->|Ramo A - Richiesta Informazioni| IA_VAL
    INTENT -->|Ramo B - Registrazione| RV

    subgraph INFO_FLOW["Ramo A – Richiesta Informazioni"]
        IA_VAL{Valida nome, email, telefono}
        IA_VAL -->|Errori| IA_ERR["Render form Errore\n— EXIT —"]
        IA_VAL -->|OK| IA_LEAD["Crea CRM lead\nRichiesta informazioni"]
        IA_LEAD --> IA_ACT["Attivita ToDo su lead\nper assegnatari CRM"]
        IA_ACT --> IA_RDR["Redirect ?info_sent=1"]
    end

    subgraph REG_FLOW["Ramo B – Registrazione"]
        RV{Valida campi obbligatori\nIBAN, CF duplicato\npassword, privacy}
        RV -->|Errori| RV_ERR["Render form Errore\n— EXIT —"]
        RV -->|OK| SU["do_signup()\ncrea res.users, commit, authenticate"]
        SU --> FP["Recupera partner da session.uid"]

        FP --> S3A["is_amministratore = True\nis_amministratore_validato = False"]
        S3A --> S3B["privacy_accepted = True\nlingua = it_IT"]
        S3B --> S4["Anagrafici:\nvia, citta, CAP, tel\nCF in savepoint isolato"]
        S4 --> S4C["Extra fields:\npec_mail, applicativo\nsistema_pagamento, foro_competente"]
        S4C --> S4D["website_id collegato\nal sito BuildingPay"]
        S4D --> S56["P.IVA normalizzata IT\ncountry_id + state_id"]
        S56 --> S7["referrer_id\ncollegamento referrer"]
        S7 --> S8["IBAN salvato in\nres.partner.bank\n(savepoint isolato)"]

        S8 --> PA1["Email benvenuto"]
        PA1 --> PA2["Attivita ToDo\nNuovo admin da validare\n(fino a 4 assegnatari)"]
        PA2 --> PA3["Task delivery\nnel Progetto BuildingPay\nresponsabile delivery"]
        PA3 --> PA4["CRM lead\nAmministratore registrato\ntag BuildingPay"]
        PA4 --> RDR_HOME["Redirect /my/home"]
    end
```

---

## 2. Flusso Portale (post-login)

```mermaid
flowchart TD
    HOME(["/my/home"]) --> ADM{is_amministratore?}
    ADM -->|No| STDP[Home portale Odoo standard]
    ADM -->|Sì| HBPSEC["Sezioni BuildingPay visibili: Contratti · Condomini · Profilo"]

    HBPSEC --> NAV{Navigazione}
    NAV --> PRF["/my/profilo"]
    NAV --> CTR["/my/contratti"]
    NAV --> CON["/my/condomini"]

    subgraph PROFILO["Profilo /my/profilo"]
        P_SHOW["Mostra: CF, P.IVA, PEC, IBAN foro_competente, applicativo sistema_pagamento, note"]
        P_SHOW --> P_POST["POST /my/profilo/save"]
        P_POST --> P_IBAN{IBAN valido?}
        P_IBAN -->|No| P_ERR[Redirect ?error=iban_invalid]
        P_IBAN -->|OK| P_WR["Aggiorna partner res.partner.bank crea / aggiorna IBAN"]
        P_WR --> P_OK[Redirect ?success=1]
    end

    subgraph CONTRATTI["Contratti /my/contratti"]
        CT_SHOW["Carica flag da partner accordo_retrocessioni_forzato accordo_condomini_forzato"]

        CT_SHOW --> C1H{accordo_retrocessioni _forzato?}
        C1H -->|Sì| C1_FORCE["Card Accordo Amm. nascosta 'Elaborato dal nostro ufficio'"]
        C1H -->|No| C1_ED{accordo_retrocessioni _ed?}
        C1_ED -->|No| C1_DL["Download PDF (NOME / CF / IBAN / ABI / CAB / DATA compilati da partner)"]
        C1_DL --> C1_UL["Upload file firmato /retrocessioni/upload"]
        C1_UL --> C1_SET["accordo_retrocessioni_ed = True Attività ToDo assegnatari (fino a 4)"]
        C1_ED -->|Sì| C1_DONE["Mostra file caricato + data upload"]

        CT_SHOW --> C2H{accordo_condomini _forzato?}
        C2H -->|Sì| C2_FORCE["Card Condomini nascosta 'Elaborato dal nostro ufficio'"]
        C2H -->|No| C2_ED{accordo_condomini _aggregati_ed?}
        C2_ED -->|No| C2_DL["Download PDF (NOME / CF / Allegato A [foro] → foro_competente o 50 underscore se vuoto)"]
        C2_DL --> C2_UL["Upload file firmato /condomini-aggregati/upload"]
        C2_UL --> C2_SET["accordo_condomini_aggregati_ed = True"]
        C2_ED -->|Sì| C2_DONE["Mostra file caricato"]
    end

    subgraph BACKOFFICE["BackOffice — group_buildingpay_manager"]
        BO_VAL["Imposta is_amministratore_validato = True (abilita creazione condomini)"]

        BO1_ON["accordo_retrocessioni_forzato = ON"]
        BO1_ON --> BO1_SET["accordo_retrocessioni_ed = True log chatter data/utente campo PeS upload visibile"]
        BO1_OFF["accordo_retrocessioni_forzato = OFF"]
        BO1_OFF --> BO1_CHK{File caricato dall'admin?}
        BO1_CHK -->|No| BO1_RST["accordo_retrocessioni_ed = False log chatter"]
        BO1_CHK -->|Sì| BO1_KEEP["_ed rimane True log chatter"]

        BO2_ON["accordo_condomini_forzato = ON"]
        BO2_ON --> BO2_SET["accordo_condomini_aggregati_ed = True log chatter data/utente campo PeS upload visibile"]
        BO2_OFF["accordo_condomini_forzato = OFF"]
        BO2_OFF --> BO2_CHK{File caricato dall'admin?}
        BO2_CHK -->|No| BO2_RST["accordo_condomini_aggregati_ed = False log chatter"]
        BO2_CHK -->|Sì| BO2_KEEP["_ed rimane True log chatter"]

        BO_COND["Condizioni economiche bp_* reset da Config BuildingPay (pulsante singolo o azione massiva)"]
    end

    subgraph CONDOMINI["Condomini /my/condomini"]
        CN_LIST["Lista condomini attivi (parent_id = partner, type = condominio)"]
        CN_LIST --> CN_ACT{Azione}
        CN_ACT --> CN_NEW["Nuovo /my/condomini/new/save"]
        CN_ACT --> CN_EDT["Modifica /my/condomini/id/save"]
        CN_ACT --> CN_ARC["Archivia /my/condomini/id/archive"]

        CN_NEW --> CN_VAL{Valida: nome, via città, CAP, CF IBAN principale}
        CN_VAL -->|Errori| CN_FORM[Ri-mostra form con errori]
        CN_VAL -->|OK| CN_CRE["Crea res.partner (type = condominio) IBAN principale + secondario flag e-invoice se cod.dest. presente"]
        CN_CRE --> CN_OK[Redirect ?success_add=1]

        CN_EDT --> CN_UPD["Aggiorna dati IBAN principale / secondario (rimuove IBAN2 se svuotato)"]
        CN_ARC --> CN_DEL["active = False archiviazione soft"]
    end
```

---

## Legenda relazioni chiave

| Elemento | Modello / Campo | Dove agisce |
|---|---|---|
| `is_amministratore` | `res.partner` | Gate di accesso a tutte le sezioni portale BP |
| `is_amministratore_validato` | `res.partner` | Impostato `False` al signup; `True` da backoffice dopo verifica |
| `foro_competente` | `res.partner` | Compilato al signup / profilo → sostituisce `[foro]` nel PDF Condomini |
| `bp_costo_email`, `bp_costo_rendicontazione`, `bp_costo_whatsapp`, `bp_quota_fissa` | `res.partner` | Default da Config BP al signup; personalizzabili per admin; reset massivo |
| `accordo_retrocessioni_forzato` | `res.partner` | Solo backoffice → nasconde card portale + setta `_ed=True` + log chatter |
| `accordo_condomini_forzato` | `res.partner` | Solo backoffice → nasconde card portale + setta `_ed=True` + log chatter |
| `project_id` + `delivery_responsible_id` | `buildingpay_v36.config` | Usati al signup per creare il task delivery automatico |
| CRM lead | `crm.lead` | Creato per richieste info (intent=info) e per ogni nuova registrazione |
| Task delivery | `project.task` | Creato nel progetto BP configurato al momento del signup |

---

## File di riferimento nel modulo

| File | Responsabilità |
|---|---|
| `controllers/portal_auth.py` | Signup, validazioni, do_signup, setup partner, azioni post-registrazione |
| `controllers/portal_main.py` | Portale: contratti, profilo, condomini |
| `models/res_partner.py` | Campi custom, `create()` / `write()` con logica force flag e default bp_* |
| `models/buildingpay_config.py` | Configurazione singleton per sito web |
| `templates/portal_registration.xml` | Form signup (referrer, foro_competente, layout CAP/Città/Provincia) |
| `templates/portal_contratto.xml` | Render contratti con gestione flag forzatura |
| `templates/portal_profilo.xml` | Form modifica dati personali |
| `templates/portal_condomini.xml` | Lista e form condomini |
