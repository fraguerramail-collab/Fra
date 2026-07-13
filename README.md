# Gestione Turni

Applicazione web (Flask + SQLite) per generare automaticamente i turni di
lavoro mensili, pensata per contesti con regole complesse (es. reparti
ospedalieri) dove disponibilità e vincoli cambiano mese per mese.

## Cosa gestisce

- **Dipendenti**: codice, nome, fascia/categoria, skill (es. `PR,SR`).
- **Tipi di turno**, ciascuno configurabile con:
  - fasce orarie (mattina/pomeriggio/notte) per verificare sovrapposizioni;
  - turno "esclusivo" (nessun altro turno lo stesso giorno, es. una notte);
  - riposo obbligatorio il giorno dopo (smonto), con un'eccezione opzionale
    per il passaggio sabato→domenica;
  - distanza minima in giorni tra due occorrenze dello stesso turno;
  - skill richiesta (con OR, es. `PR|SR`) e categorie escluse (es. `SP`);
  - turno "a blocco settimanale" (assegnato alla stessa persona per tutta
    la settimana, es. corsia);
  - turno "extra", attivabile solo su date specifiche con un pool di
    equità separato dal carico normale;
  - fabbisogno di personale per ogni giorno della settimana.
- **Weekend a ruoli**: definisci ruoli (es. A/B/C/D) in cui lo stesso
  dipendente copre più turni tra sabato e domenica.
- **Preferenze** pesate: EVITA / PREFERISCI / RISERVA / MAX_MESE, per
  dipendente, turno e giorni specifici.
- **Disponibilità mensile** granulare: disponibile, non disponibile
  (intera giornata o solo mattina/pomeriggio/notte), ferie, malattia.
- **Soppressioni**: disattiva un turno obbligatorio in una data specifica.
- **Turni extra**: attiva un turno "extra" su una data puntuale.
- **Generazione automatica**: assegna i turni rispettando tutte le regole
  sopra, con equità del carico (anche rispetto al mese precedente per
  smonto e weekend) e segnala eventuali giorni scoperti.
- **Report** con conteggi e avvisi per dipendente, **export CSV** ed
  editing manuale del piano generato.

## Profili (reparti multipli)

L'app supporta più "profili" (es. reparti diversi), ciascuno con il proprio
database SQLite completamente separato: dipendenti, turni, regole, weekend,
disponibilità e piani generati di un profilo non hanno alcun effetto sugli
altri. Aprendo l'app si arriva a una pagina di scelta del reparto; da lì si
può entrare in un reparto esistente o crearne uno nuovo (viene inizializzato
subito con un catalogo turni di esempio, da adattare). Ogni reparto vive
sotto `/p/<slug>/`.

## Avvio in locale

```bash
pip install -r requirements.txt
python run.py
```

L'app parte su `http://localhost:5000` con la pagina di scelta del reparto.
I database SQLite vengono creati automaticamente in `instance/data_<slug>.db`
alla prima apertura di ciascun reparto, con alcuni tipi di turno di esempio;
l'elenco dei reparti è in `instance/profiles.json`.

## Test

```bash
pip install pytest
python -m pytest tests/
```

## Flusso d'uso consigliato

1. Configura **Tipi di turno** e le loro regole.
2. Se necessario, configura **Weekend** (ruoli) e **Preferenze**.
3. Aggiungi i **Dipendenti** con le rispettive skill.
4. Ogni mese: inserisci **Disponibilità**, eventuali **Soppressioni** e
   **Turni extra**.
5. Vai in **Pianificazione**, genera il mese, correggi manualmente se
   serve, controlla il **Report** ed esporta in CSV.

## Note

Il server di sviluppo integrato di Flask non è adatto alla produzione.
Per un uso reale (accessibile da altri computer) va distribuito con un
server WSGI (es. gunicorn) dietro un reverse proxy, o su una piattaforma
di hosting.
