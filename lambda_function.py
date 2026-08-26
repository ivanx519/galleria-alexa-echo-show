import copy
import logging
import json
import urllib.request
import urllib.parse

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractExceptionHandler, AbstractRequestInterceptor
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective, ExecuteCommandsDirective

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOT_URL = "http://YOUR-SERVER-IP/foto-slideshow-api"
BOT_TOKEN = "CHANGE-ME-your-own-secret-token"

# Le chiamate Lambda -> server (fetch dati) restano su BOT_URL (HTTP, va bene
# per traffico server-to-server). L'URL delle immagini invece viene scaricato
# DIRETTAMENTE dal dispositivo Echo Show tramite il componente Image di APL —
# bug trovato dal vivo il 21/08/2026: con l'IP in HTTP semplice le foto
# restavano nere (mai caricate), Alexa richiede HTTPS per le risorse
# caricate dai componenti APL. Stesso dominio già usato per copytalk/geo-api.
BOT_URL_HTTPS = "https://YOUR-DOMAIN.example.com/foto-slideshow-api"

# Quante foto precaricare per blocco — abbastanza da coprire diversi minuti di
# slideshow prima di dover richiamare la Lambda (che è l'unico momento in cui
# il microfono si riattiva per qualche secondo, vedi design qui sotto). Il
# 22/08/2026 le icone vettoriali (ingranaggio, frecce, orologio) erano state
# tolte e sostituite con primitive semplici (puntini, testo "‹"/"›") su
# richiesta di Ivan ("estremamente minimal"), recuperando ~3 KB sul
# documento statico — margine che aveva permesso di tornare da 55 (ridotto
# in via cautelativa) fino a 65. Il 24/08/2026 la dissolvenza tra foto e il
# pannello impostazioni durata sono stati aggiunti e poi tolti di nuovo lo
# stesso giorno (su richiesta di Ivan, dopo un primo assaggio dal vivo) —
# 65 resta il tetto verificato con DATI REALI (didascalie vere dalla VPS,
# non sintetiche, incluso il caso peggiore: 65 foto tutte con la didascalia
# più lunga mai vista, "Trinità d'Agultu e Vignola · Luglio 2024") per
# restare sotto i 24.576 byte di risposta massima di Alexa — indipendente
# dal numero totale di foto in libreria, è solo quante ne carica insieme.
DIMENSIONE_BLOCCO = 65

# Fase 1 video (23/08/2026): "isola" separata dalle foto, comando vocale
# dedicato "mostra i video" — Pager solo video con autoplay + onEnd invece
# di un timer fisso (AutoPage non supporta durate diverse per pagina, un
# video ha una lunghezza propria, foto e video NON sono mescolati insieme
# nello stesso ciclo per questo motivo, vedi ricerca fatta il 23/08/2026).
# Verificato col vero budget byte (23/08/2026, nomi file reali dalla VPS):
# il documento video è molto più leggero di quello foto (niente didascalie,
# icone, pannello impostazioni) — margine ~18 KB anche con 40 elementi,
# ben più ampio di quello delle foto (~1,9 KB). Deve combaciare con
# BATCH_MASSIMO_VIDEO in server.py.
DIMENSIONE_BLOCCO_VIDEO = 40

# Fase 2 (23/08/2026): "mostra foto e video" — blocco misto, un elemento
# ogni RAPPORTO_VIDEO_MISTO è un video (vedi server.py). Documento più
# pesante di quello video-solo (contiene ANCHE il blocco foto con le due
# Image sovrapposte) — valore di partenza, da verificare col vero budget
# byte prima del deploy, deve combaciare con BATCH_MASSIMO_MISTO in server.py.
DIMENSIONE_BLOCCO_MISTO = 40

FREEZER_BLUE = "#4FC3F7"  # coerenza con la palette già usata in Freezer/IronWoman


def _get(path, params=None):
    qs = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"{BOT_URL}{path}{qs}",
        headers={"Authorization": f"Bearer {BOT_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path, body):
    # Per ora usato solo dal tasto preferiti (24/08/2026) — corpo piccolo,
    # nessun bisogno di gestire risposte grandi o timeout più larghi di _get.
    req = urllib.request.Request(
        f"{BOT_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _con_apl(handler_input):
    system = handler_input.request_envelope.context.system
    device = getattr(system, "device", None)
    interfaces = getattr(device, "supported_interfaces", None) if device else None
    return bool(interfaces) and getattr(interfaces, "alexa_presentation_apl", None) is not None


def _url_immagine(nome_file):
    # Il componente Image di APL fa una richiesta HTTP semplice, senza poter
    # aggiungere un header Authorization — token passato in query string
    # come alternativa, solo per questa rotta (vedi server.py, _check_auth_binario).
    # HTTPS obbligatorio qui (vedi BOT_URL_HTTPS): il dispositivo scarica
    # l'immagine direttamente, non passa dalla Lambda.
    return f"{BOT_URL_HTTPS}/api/foto/binario/{urllib.parse.quote(nome_file)}?token={BOT_TOKEN}"


def _url_video(nome_file):
    # Stesso principio di _url_immagine: il componente Video di APL scarica
    # (in streaming) direttamente dal dispositivo, token in query string.
    return f"{BOT_URL_HTTPS}/api/video/binario/{urllib.parse.quote(nome_file)}?token={BOT_TOKEN}"


def _costruisci_datasource(foto, durata_secondi, termine_ricerca=""):
    return {
        "slideshowData": {
            "type": "object",
            "properties": {
                "foto": [
                    {
                        "nome": f["nome"], "url": _url_immagine(f["nome"]), "didascalia": f.get("didascalia", ""),
                        "preferita": f.get("preferita", False),
                    }
                    for f in foto
                ],
                "durataSecondi": durata_secondi,
                "durataMs": durata_secondi * 1000,
                # Vuoto = galleria normale. Non vuoto = "sto mostrando i
                # risultati di questa ricerca" — vedi Bind "termineRicercaLocale".
                "termineRicerca": termine_ricerca,
            },
        }
    }


# --- Comandi eseguiti automaticamente ad ogni apertura/aggiornamento del
# documento (vedi "onMount" più sotto, DENTRO l'APL_DOCUMENT — non una
# direttiva ExecuteCommands separata: prima versione, bug da race condition
# col Pager non ancora montato, vedi cronologia bug più sotto).
#
# "duration" qui è un valore PLACEHOLDER (None), sostituito con un numero
# vero per ogni render da _documento_con_durata() — un'espressione
# "${payload...}" non si risolveva mai in questo punto del documento
# (bug #2, vedi cronologia).
#
# NESSUN "sequencer" personalizzato su questi due comandi (bug #3, il più
# subdolo, trovato dal vivo il 21/08/2026 dopo che i primi due fix non
# avevano cambiato nulla): avevo taggato sia l'AutoPage che il SendEvent
# successivo con lo stesso "sequencer":"autoplay", pensando servisse per
# poterli interrompere dai pulsanti pausa/prev/succ. Ma mandare un comando
# su un sequencer nominato interrompe QUALSIASI cosa stia già girando su
# quello stesso sequencer — quindi il SendEvent, arrivando sullo stesso
# canale "autoplay" subito dopo l'AutoPage, lo cancellava da solo,
# istantaneamente, qualunque fosse la "duration" impostata (spiega perché
# cambiarla non aveva mai alcun effetto). Sia "onMount" che i tocchi
# (onPress) usano di default lo stesso sequencer principale — non serve
# nominarlo per poterlo interrompere dai pulsanti, succede comunque perché
# competono tutti per lo stesso canale predefinito.
_COMANDI_AUTOPLAY_ONMOUNT = [
    {
        "type": "AutoPage",
        "componentId": "fotoPager",
        "duration": None,
        "count": 999,
        # Disabilita il timer di inattività dell'Echo Show per tutta la
        # durata di questo comando (l'intero avanzamento del blocco di
        # foto) — senza, lo schermo si chiudeva da solo dopo pochi decine
        # di secondi perché l'avanzamento automatico è tutto lato client,
        # invisibile al timer di inattività (bug trovato dal vivo il
        # 21/08/2026: si chiudeva dopo 4-8 foto, ~40-80 secondi).
        "screenLock": True,
        # Piccolo ritardo PRIMA che l'AutoPage parta — tentativo di fix per
        # una chiusura prematura intermittente segnalata dal vivo il
        # 23/08/2026 (a volte ~45s, a volte ~4min39s, sempre
        # reason=USER_INITIATED senza interazione reale). Ipotesi: al
        # PRIMISSIMO caricamento (LaunchRequest) il documento va compilato da
        # zero (icone, layout, Pager) — un secondo render con lo STESSO
        # token "slideshowToken" (es. dicendo "mostra foto" subito dopo,
        # workaround che Ivan usava già prima della ricerca per mese/luogo)
        # riusa il documento già compilato ed è più affidabile. Questo
        # ritardo dà al primo mount qualche centesimo di secondo in più
        # prima di affidargli il comando lungo che tiene viva la sessione —
        # da verificare dal vivo, non ancora confermato come causa reale.
        "delay": 800,
    },
    {
        "type": "SendEvent",
        "arguments": ["blocco_esaurito", "${termineRicercaLocale}"],
    },
]


# Icone vettoriali proprie (formato AVG, nativo APL dalla versione 1.4/1.5).
# Ridisegno "estremamente minimal" del 22/08/2026 (su richiesta esplicita di
# Ivan, dopo il mockup con vetro+icone): tolte le icone per impostazioni,
# precedente/successiva e orologio — sostituite con primitive semplici
# (puntini, testo "‹"/"›") che non richiedono grafica affatto. Resta solo
# play/pausa: un triangolo pieno non si disegna in modo affidabile con le
# primitive APL (Frame ha bordi uniformi, non per-lato come il CSS), quindi
# qui la tecnologia AVG già collaudata dal vivo resta la scelta più sicura —
# ma ridotta a due soli path minimi. Da 6 icone (2.445 byte totali) a 2
# (437 byte): risparmio diretto di ~2.000 byte sul budget della risposta.
def _icona(*pezzi):
    return {"type": "AVG", "version": "1.0", "height": 24, "width": 24,
            "viewportHeight": 24, "viewportWidth": 24, "items": list(pezzi)}


GRAFICHE_ICONE = {
    "iconaPlay": _icona(
        {"type": "path", "fill": "white", "pathData": "M8 5 L19 12 L8 19 Z"},
    ),
    "iconaPausa": _icona(
        {"type": "path", "fill": "white", "pathData": "M6 5 L10 5 L10 19 L6 19 Z M14 5 L18 5 L18 19 L14 19 Z"},
    ),
}


APL_DOCUMENT = {
    "type": "APL",
    "version": "1.9",
    "graphics": GRAFICHE_ICONE,
    # Parte da solo quando il documento è pronto (Pager incluso) — vedi nota
    # sopra su _COMANDI_AUTOPLAY_ONMOUNT sul perché non è più una direttiva
    # ExecuteCommands separata mandata dalla Lambda.
    "onMount": _COMANDI_AUTOPLAY_ONMOUNT,
    "mainTemplate": {
        "parameters": ["payload"],
        "items": [
            {
                "type": "Frame",
                "id": "root",
                "position": "absolute",
                "top": 0, "left": 0, "right": 0, "bottom": 0,
                "backgroundColor": "#000000",
                # Stato locale — tutto qui vive SOLO sullo schermo, nessuna
                # di queste azioni richiama la Lambda.
                "bind": [
                    {"name": "controlliVisibili", "type": "boolean", "value": False},
                    {"name": "inPausaLocale", "type": "boolean", "value": False},
                    {"name": "durataSecondiLocale", "type": "number", "value": "${payload.slideshowData.properties.durataSecondi}"},
                    # Indice della foto corrente, aggiornato via "onPageChanged"
                    # sul Pager (vedi sotto) invece di leggere "fotoPager.currentPage"
                    # direttamente da un componente sorella — quel riferimento diretto
                    # non si aggiornava in modo affidabile durante l'avanzamento
                    # AUTOMATICO (AutoPage), causando la didascalia bloccata sulla
                    # stessa foto per minuti anche se le immagini scorrevano
                    # normalmente (bug segnalato dal vivo il 22/08/2026). Il valore
                    # iniziale 0 combacia con la prima pagina mostrata dal Pager.
                    {"name": "paginaCorrenteLocale", "type": "number", "value": 0},
                    # Se non vuoto, significa "sto mostrando i risultati della
                    # ricerca X, non la galleria normale" — va portato dietro
                    # ad ogni "blocco_esaurito" (vedi sotto) così la Lambda sa
                    # se continuare a pescare da /api/foto/cerca (altre foto
                    # trovate dalla stessa ricerca) o tornare a /api/foto/batch
                    # (galleria normale). Segnalato dal vivo il 22/08/2026:
                    # senza questo, finite le prime foto di una ricerca si
                    # tornava silenziosamente alla galleria mescolata invece
                    # di continuare con le altre foto trovate.
                    {"name": "termineRicercaLocale", "type": "string", "value": "${payload.slideshowData.properties.termineRicerca}"},
                    # Feedback istantaneo del tasto preferiti (25/08/2026) —
                    # segnalato dal vivo: il cuoricino non cambiava subito al
                    # tap perché legge lo stato da payload (si aggiorna solo
                    # al render/blocco successivo, scelta deliberata per non
                    # toccare le liste di comandi di navigazione già delicate
                    # su questo file). Fix a basso rischio: due bind SOLO per
                    # "l'ultimo tap", letti PRIMA del payload nell'icona (vedi
                    # sotto) — tornano irrilevanti da soli appena l'indice
                    # cambia (indice diverso = niente override), nessuna
                    # lista di comandi esistente da tenere sincronizzata.
                    {"name": "preferitaOverrideIndice", "type": "number", "value": -1},
                    {"name": "preferitaOverrideValore", "type": "boolean", "value": False},
                ],
                "item": {
                    "type": "Container",
                    "width": "100%",
                    "height": "100%",
                    "items": [
                        {
                            "type": "Pager",
                            "id": "fotoPager",
                            "width": "100%",
                            "height": "100%",
                            "navigation": "normal",
                            "data": "${payload.slideshowData.properties.foto}",
                            # Aggiorna il Bind "paginaCorrenteLocale" ogni volta che
                            # il Pager finisce di cambiare pagina — sia per swipe
                            # manuale sia per avanzamento automatico (AutoPage).
                            # Meccanismo dedicato e documentato per questo, più
                            # affidabile del riferimento diretto "fotoPager.currentPage"
                            # da un componente sorella (vedi nota sul Bind sopra).
                            "onPageChanged": [
                                {
                                    "type": "SetValue",
                                    "componentId": "root",
                                    "property": "paginaCorrenteLocale",
                                    "value": "${event.source.page}",
                                }
                            ],
                            "item": {
                                "type": "TouchWrapper",
                                "width": "100%",
                                "height": "100%",
                                # Tocco sulla foto: mostra/nascondi i controlli.
                                # DUE bug corretti qui il 22/08/2026, segnalati dal
                                # vivo ("non c'è il tasto impostazioni... se tocco lo
                                # schermo non scorre più la foto e poi si chiude"):
                                #
                                # Bug 1 — controlli invisibili: prima, "Idle" (attesa
                                # 4s) e il "SetValue" che nasconde erano DUE comandi
                                # separati mandati sullo stesso sequencer "autohide"
                                # nello stesso tocco — il secondo, arrivando sullo
                                # stesso canale dell'"Idle" ancora in corso, lo
                                # annullava ISTANTANEAMENTE (stesso meccanismo del
                                # bug #3 già visto su questa skill). I controlli
                                # comparivano e sparivano nello stesso istante, mai
                                # visibili abbastanza per essere toccati. Fix: un
                                # SOLO comando con "delay" incorporato — non c'è più
                                # nulla da cancellare a vicenda.
                                #
                                # Bug 2 — lo scorrimento si fermava al tocco: questi
                                # comandi giravano sul sequencer MAIN di default, lo
                                # STESSO usato dall'AutoPage (scelta deliberata per
                                # i pulsanti prev/pausa/succ, che DEVONO interrompere
                                # l'AutoPage) — ma qui il tocco serve solo a
                                # sbirciare i controlli, non a fermare lo slideshow.
                                # Su MAIN, interrompeva comunque l'AutoPage in corso;
                                # una volta fermo, "screenLock" non era più attivo e
                                # lo schermo si chiudeva da solo per inattività poco
                                # dopo. Fix: sequencer dedicato "controlliTocco",
                                # separato da MAIN — il tocco ora mostra/nasconde i
                                # controlli SENZA toccare l'AutoPage in corso.
                                "onPress": [
                                    {
                                        "type": "SetValue",
                                        "sequencer": "controlliTocco",
                                        "componentId": "root",
                                        "property": "controlliVisibili",
                                        "value": "${!controlliVisibili}",
                                    },
                                    {
                                        "type": "SetValue",
                                        "sequencer": "controlliTocco",
                                        "componentId": "root",
                                        "property": "controlliVisibili",
                                        "value": False,
                                        "delay": 4000,
                                        "when": "${controlliVisibili}",
                                    },
                                ],
                                "item": {
                                    # Foto verticali su schermo orizzontale: prima una
                                    # sola Image con "best-fill" tagliava via pezzi
                                    # della foto (segnalato dal vivo il 22/08/2026 —
                                    # sembrava un blocco nero, in realtà era un
                                    # ritaglio troppo aggressivo). Fix standard "stile
                                    # cornice digitale": sotto la stessa foto ingrandita
                                    # e sfocata riempie tutto lo schermo, sopra la foto
                                    # INTERA (mai tagliata, "best-fit") sta al centro —
                                    # i lati (o sopra/sotto) si riempiono con lo sfondo
                                    # sfocato invece che con un taglio o una barra nera.
                                    "type": "Container",
                                    "width": "100%",
                                    "height": "100%",
                                    "items": [
                                        {
                                            "type": "Image",
                                            "source": "${data.url}",
                                            "position": "absolute",
                                            "width": "100%",
                                            "height": "100%",
                                            "scale": "best-fill",
                                            "align": "center",
                                            "filters": [{"type": "Blur", "radius": "30dp"}],
                                        },
                                        {
                                            "type": "Image",
                                            "source": "${data.url}",
                                            "position": "absolute",
                                            "width": "100%",
                                            "height": "100%",
                                            "scale": "best-fit",
                                            "align": "center",
                                        },
                                    ],
                                },
                            },
                        },
                        {
                            # Didascalia ambient (stato "a riposo") — in basso a
                            # sinistra, sempre visibile se presente (indipendente
                            # dai controlli, come nel mockup approvato).
                            # PRIMA usava "when" su foto[0].didascalia (bug #1,
                            # fisso valutato una sola volta — vedi cronologia bug
                            # su Notion), POI leggeva "fotoPager.currentPage"
                            # direttamente da un componente sorella (bug #2: quel
                            # riferimento non si aggiornava in modo affidabile
                            # durante l'avanzamento AUTOMATICO — la scritta restava
                            # bloccata sulla stessa foto per minuti nonostante le
                            # immagini scorressero regolarmente, segnalato dal vivo
                            # il 22/08/2026). Fix: legge "paginaCorrenteLocale",
                            # aggiornato in modo esplicito da "onPageChanged" sul
                            # Pager (vedi sopra) — un meccanismo pensato apposta
                            # per essere affidabile sia su swipe manuale sia su
                            # avanzamento automatico.
                            # Leggibilità su qualsiasi foto: prima un cartellino scuro
                            # dietro il testo (bocciato il 22/08/2026, "taglia" troppo
                            # la foto), scelto invece — dopo un confronto visivo con
                            # varie alternative — solo un'ombra scura forte intorno al
                            # testo, senza sfondo: zero ingombro sulla foto, contrasto
                            # comunque garantito nella grande maggioranza dei casi.
                            # "shadow*" è una proprietà base dei componenti APL dalla
                            # 1.7 in poi (qui in uso la 1.9) — per il Text segue la
                            # forma stessa delle lettere, non un rettangolo.
                            "type": "Text",
                            "position": "absolute",
                            "left": "31dp",
                            "bottom": "31dp",
                            "display": "${payload.slideshowData.properties.foto[paginaCorrenteLocale].didascalia != '' ? 'normal' : 'none'}",
                            "text": "${payload.slideshowData.properties.foto[paginaCorrenteLocale].didascalia}",
                            "color": "white",
                            "fontSize": "38dp",
                            "fontWeight": "600",
                            "shadowColor": "rgba(0,0,0,0.9)",
                            "shadowOffsetX": 0,
                            "shadowOffsetY": 0,
                            "shadowRadius": 8,
                        },
                        # --- Overlay controlli (stato "dopo un tocco") — due giri di
                        # ridisegno con Ivan il 22/08/2026: prima da "fascio nero" a
                        # vetro scuro con icone vettoriali (3 mockup), poi da lì a
                        # "estremamente minimal" (puntini, testo "‹"/"›", nessuno
                        # sfondo/capsula/bordo) per recuperare margine byte — vedi nota
                        # su GRAFICHE_ICONE. Niente barra di avanzamento in nessuno dei
                        # due (tolta su richiesta, "non mi interessa sapere a che punto
                        # sono"). Il pulsante impostazioni (tre puntini) e il pannello
                        # "durata per foto" sono stati tolti il 24/08/2026 su richiesta
                        # di Ivan — restano solo frecce/pausa-play, niente più timer
                        # configurabile dall'utente.
                        {
                            # Stesso bug/fix di layout già noto su questa skill: qui
                            # "alignItems": "center" (asse orizzontale, direzione di
                            # default "column") su un contenitore a piena larghezza
                            # centra la capsula sottostante, che invece resta
                            # libera di restringersi al proprio contenuto (nessuna
                            # "width" su di essa) — è quello che la fa sembrare una
                            # vera capsula "fluttuante" e non una barra intera.
                            "type": "Container",
                            "display": "${controlliVisibili ? 'normal' : 'none'}",
                            "position": "absolute", "bottom": 0, "left": 0, "right": 0,
                            "width": "100%",
                            "alignItems": "center",
                            "paddingBottom": "24dp",
                            # Ridisegno "estremamente minimal" del 22/08/2026: via la
                            # capsula in vetro e i cerchi dietro le frecce — restano solo
                            # i tre segni, sospesi, senza alcuno sfondo/bordo/ombra.
                            "items": [
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "alignItems": "center",
                                    "items": [
                                        {
                                            # Precedente: annulla l'autoplay in corso (stesso
                                            # sequencer) e sposta il Pager di una pagina indietro.
                                            # Al bordo (prima foto del blocco) non fa nulla — non
                                            # c'è un blocco precedente caricato, limite noto.
                                            "type": "TouchWrapper",
                                            "paddingLeft": "20dp", "paddingRight": "20dp",
                                            "paddingTop": "16dp", "paddingBottom": "16dp",
                                            "onPress": [
                                                # "Idle" su "autoplay" senza fare nulla: il solo
                                                # fatto di mandare un comando su questo sequencer
                                                # annulla l'AutoPage in corso (nessun comando
                                                # "stop" dedicato in APL, questo è l'equivalente).
                                                {"type": "Idle", "delay": 0},
                                                {"type": "SetPage", "componentId": "fotoPager", "position": "relative", "value": -1},
                                                {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": True},
                                            ],
                                            # "‹" — carattere di punteggiatura standard, non
                                            # un'icona: nessun dato grafico da caricare.
                                            "item": {
                                                "type": "Text",
                                                "text": "‹",
                                                "fontSize": "34dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "34dp", "paddingRight": "34dp",
                                            "paddingTop": "16dp", "paddingBottom": "16dp",
                                            "onPress": [
                                                {
                                                    # Inverte lo stato pausa. I comandi seguenti leggono
                                                    # il valore GIÀ aggiornato (ogni comando in sequenza
                                                    # vede lo stato al momento in cui tocca a lui).
                                                    "type": "SetValue", "componentId": "root",
                                                    "property": "inPausaLocale", "value": "${!inPausaLocale}",
                                                },
                                                {
                                                    # Appena messo in pausa: annulla l'AutoPage in corso
                                                    # (mandare un comando qualsiasi sul sequencer principale,
                                                    # lo stesso che usa "onMount", interrompe quello attivo).
                                                    "type": "Idle", "delay": 0,
                                                    "when": "${inPausaLocale}",
                                                },
                                                {
                                                    # Appena ripreso: riparte da qui in avanti (il Pager
                                                    # è già sulla pagina dove l'utente l'aveva lasciato).
                                                    "type": "AutoPage",
                                                    "componentId": "fotoPager",
                                                    "duration": "${durataSecondiLocale * 1000}",
                                                    "count": 999,
                                                    "when": "${!inPausaLocale}",
                                                    "screenLock": True,
                                                },
                                                {
                                                    "type": "SendEvent",
                                                    "when": "${!inPausaLocale}",
                                                    "arguments": ["blocco_esaurito", "${termineRicercaLocale}"],
                                                },
                                            ],
                                            # Unico glifo rimasto con grafica vettoriale (un
                                            # triangolo pieno non si disegna in modo affidabile
                                            # con le primitive APL) — ma ridotto a due path
                                            # minimi, vedi nota su GRAFICHE_ICONE.
                                            "item": {
                                                "type": "VectorGraphic",
                                                "source": "${inPausaLocale ? 'iconaPlay' : 'iconaPausa'}",
                                                "width": "22dp", "height": "22dp",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "20dp", "paddingRight": "20dp",
                                            "paddingTop": "16dp", "paddingBottom": "16dp",
                                            "onPress": [
                                                {"type": "Idle", "delay": 0},
                                                {"type": "SetPage", "componentId": "fotoPager", "position": "relative", "value": 1},
                                                {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": True},
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "›",
                                                "fontSize": "34dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            # Preferiti (24/08/2026) — legge lo stato
                                            # direttamente dal payload del blocco corrente
                                            # (paginaCorrenteLocale, già affidabile e in
                                            # sync — vedi bind sopra). Feedback istantaneo
                                            # (25/08/2026, segnalato dal vivo) via
                                            # preferitaOverrideIndice/Valore — vedi nota
                                            # sul bind: cambia icona SUBITO al tap, senza
                                            # toccare le liste di comandi di navigazione.
                                            "type": "TouchWrapper",
                                            "paddingLeft": "20dp", "paddingRight": "20dp",
                                            "paddingTop": "16dp", "paddingBottom": "16dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue", "componentId": "root",
                                                    "property": "preferitaOverrideIndice",
                                                    "value": "${paginaCorrenteLocale}",
                                                },
                                                {
                                                    "type": "SetValue", "componentId": "root",
                                                    "property": "preferitaOverrideValore",
                                                    "value": "${!payload.slideshowData.properties.foto[paginaCorrenteLocale].preferita}",
                                                },
                                                {
                                                    "type": "SendEvent",
                                                    "arguments": [
                                                        "toggla_preferito", "foto",
                                                        "${payload.slideshowData.properties.foto[paginaCorrenteLocale].nome}",
                                                        "${!payload.slideshowData.properties.foto[paginaCorrenteLocale].preferita}",
                                                    ],
                                                },
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "${(preferitaOverrideIndice == paginaCorrenteLocale ? preferitaOverrideValore : payload.slideshowData.properties.foto[paginaCorrenteLocale].preferita) ? '❤' : '♡'}",
                                                "fontSize": "30dp",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
            }
        ],
    },
}


# --- Documento APL per i video (Fase 1, 23/08/2026) — deliberatamente
# separato da APL_DOCUMENT (foto), non condivide bind/onMount: qui
# l'avanzamento non è un timer fisso ("AutoPage") ma il componente Video
# stesso con "autoplay" + "onEnd", perché ogni video ha una lunghezza
# propria (AutoPage non supporta durate diverse per pagina — verificato
# nella ricerca APL fatta prima di questo lavoro). Nessuna didascalia
# (nessun geocoding sui video), nessun controllo manuale per ora — validare
# prima che il meccanismo di base (autoplay a catena + cambio blocco) sia
# affidabile dal vivo, poi aggiungere pausa/precedente/successivo.
#
# NIENTE Pager: il primo tentativo (23/08/2026) usava un Pager con un
# componente Video per pagina, "autoplay":true sul template — bug trovato
# dal vivo, segnalato come "il video si frizza e sento l'audio di un altro
# video": un Pager tiene MONTATE (per lo scorrimento fluido) anche le
# pagine adiacenti a quella corrente, quindi PIÙ componenti Video con
# autoplay partivano insieme, ognuno con la propria colonna audio — si
# sentiva quello sbagliato mentre lo schermo mostrava (o restava fermo su)
# un altro. Fix: un SOLO componente Video nell'intero documento, la cui
# "source" viene semplicemente ri-bindata su un indice che avanza — mai
# più di un video montato/in autoplay alla volta, per costruzione.
# Comandi per passare al video successivo (o chiedere il prossimo blocco se
# questo è finito) — condivisi tra "onEnd" (fine naturale) e il tocco
# manuale (vedi TouchWrapper sotto), stessa logica in entrambi i casi.
_COMANDI_AVANZA_VIDEO = [
    {
        "type": "SetValue",
        "componentId": "rootVideo",
        "property": "indiceVideoLocale",
        "value": "${indiceVideoLocale + 1}",
        "when": "${indiceVideoLocale < payload.videoSlideshowData.properties.totaleVideo - 1}",
    },
    # Avanzare implica voler vedere il nuovo video, non restare in pausa
    # sul vecchio stato — coerente con l'icona pausa/play nella barra.
    {"type": "SetValue", "componentId": "rootVideo", "property": "inPausaLocaleVideo", "value": False},
    {
        # "autoplay" scatta solo al PRIMO montaggio del componente Video —
        # ri-bindare "source" su un componente già montato NON lo fa
        # ripartire da solo (bug segnalato dal vivo il 23/08/2026: dal
        # secondo video in poi si vedeva solo il primo fotogramma fermo,
        # come un'immagine, mai in riproduzione). Serve dirglielo
        # esplicitamente ogni volta che si cambia video.
        "type": "ControlMedia",
        "componentId": "videoAttuale",
        "command": "play",
        "when": "${indiceVideoLocale < payload.videoSlideshowData.properties.totaleVideo - 1}",
    },
    {
        "type": "SendEvent",
        "arguments": ["blocco_video_esaurito"],
        "when": "${indiceVideoLocale >= payload.videoSlideshowData.properties.totaleVideo - 1}",
    },
]

# Indietro — stessa logica speculare, richiesto da Ivan il 23/08/2026. Al
# bordo (primo video del blocco) non fa nulla — non c'è un blocco
# precedente caricato, stesso limite noto già presente per le foto.
_COMANDI_INDIETRO_VIDEO = [
    {
        "type": "SetValue",
        "componentId": "rootVideo",
        "property": "indiceVideoLocale",
        "value": "${indiceVideoLocale - 1}",
        "when": "${indiceVideoLocale > 0}",
    },
    {"type": "SetValue", "componentId": "rootVideo", "property": "inPausaLocaleVideo", "value": False},
    {
        "type": "ControlMedia",
        "componentId": "videoAttuale",
        "command": "play",
        "when": "${indiceVideoLocale > 0}",
    },
]

APL_DOCUMENT_VIDEO = {
    "type": "APL",
    "version": "1.9",
    # Riusa le stesse icone play/pausa già definite per le foto (GRAFICHE_ICONE) —
    # stesso disegno "estremamente minimal", zero costo aggiuntivo di ridisegno.
    "graphics": GRAFICHE_ICONE,
    "mainTemplate": {
        "parameters": ["payload"],
        "items": [
            {
                "type": "Frame",
                "id": "rootVideo",
                "position": "absolute",
                "top": 0, "left": 0, "right": 0, "bottom": 0,
                "backgroundColor": "#000000",
                "bind": [
                    {"name": "indiceVideoLocale", "type": "number", "value": 0},
                    {"name": "controlliVisibiliVideo", "type": "boolean", "value": False},
                    {"name": "inPausaLocaleVideo", "type": "boolean", "value": False},
                    {"name": "videoSilenziatoLocale", "type": "boolean", "value": False},
                    # Feedback istantaneo del tasto preferiti (25/08/2026) —
                    # segnalato dal vivo: il cuoricino non cambiava subito al
                    # tap perché legge lo stato da payload (si aggiorna solo
                    # al render/blocco successivo, scelta deliberata per non
                    # toccare le liste di comandi di navigazione già delicate
                    # su questo file). Fix a basso rischio: due bind SOLO per
                    # "l'ultimo tap", letti PRIMA del payload nell'icona (vedi
                    # sotto) — tornano irrilevanti da soli appena l'indice
                    # cambia (indice diverso = niente override), nessuna
                    # lista di comandi esistente da tenere sincronizzata.
                    {"name": "preferitaOverrideIndice", "type": "number", "value": -1},
                    {"name": "preferitaOverrideValore", "type": "boolean", "value": False},
                ],
                "item": {
                    "type": "Container",
                    "width": "100%",
                    "height": "100%",
                    "items": [
                        {
                            "type": "Video",
                            "id": "videoAttuale",
                            "width": "100%",
                            "height": "100%",
                            "scale": "best-fit",
                            "autoplay": True,
                            "muted": "${videoSilenziatoLocale}",
                            # Dalla versione APL 2024.2 il componente Video ha
                            # "screenLock" proprio (default true) che da solo
                            # disabilita il timer di inattività/sessione
                            # durante la riproduzione — qui dichiarato
                            # esplicitamente anche se il documento usa ancora
                            # "version":"1.9" (i runtime più recenti tendono a
                            # ignorare le proprietà che non riconoscono invece
                            # di fallire, quindi il rischio di dichiararlo è
                            # basso). Bug segnalato dal vivo il 23/08/2026:
                            # la sessione si chiudeva da sola dopo ~30 secondi
                            # di video, stesso limite di piattaforma già
                            # trovato con le foto — da verificare se questo
                            # basta a risolverlo per i video.
                            "screenLock": True,
                            "source": "${payload.videoSlideshowData.properties.video[indiceVideoLocale].url}",
                            # A fine video: avanza l'indice (la "source" sopra
                            # si ri-valuta da sola e il nuovo video parte) o
                            # chiede il prossimo blocco — vedi _COMANDI_AVANZA_VIDEO.
                            "onEnd": _COMANDI_AVANZA_VIDEO,
                        },
                        {
                            # Didascalia (luogo · mese anno) — stesso stile
                            # identico delle foto (solo ombra, niente sfondo).
                            # Vuota per i video senza GPS/non ancora
                            # geocodificati (vedi geocodifica_video.py,
                            # 23/08/2026), "display" la nasconde da sola.
                            "type": "Text",
                            "position": "absolute",
                            "left": "31dp",
                            "bottom": "31dp",
                            "display": "${payload.videoSlideshowData.properties.video[indiceVideoLocale].didascalia != '' ? 'normal' : 'none'}",
                            "text": "${payload.videoSlideshowData.properties.video[indiceVideoLocale].didascalia}",
                            "color": "white",
                            "fontSize": "38dp",
                            "fontWeight": "600",
                            "shadowColor": "rgba(0,0,0,0.9)",
                            "shadowOffsetX": 0,
                            "shadowOffsetY": 0,
                            "shadowRadius": 8,
                        },
                        {
                            # Tocco per mostrare/nascondere la barra
                            # controlli — stesso identico meccanismo (un
                            # solo comando con "delay" incorporato, sequencer
                            # dedicato) già usato e collaudato per le foto,
                            # per evitare lo stesso bug di autocancellazione
                            # già trovato una volta su questa skill.
                            # Richiesto da Ivan il 23/08/2026: voleva una
                            # barra come quella delle foto (indietro, muto,
                            # pausa/play, avanti) invece di zone invisibili
                            # sempre attive.
                            "type": "TouchWrapper",
                            "position": "absolute",
                            "top": 0, "left": 0, "right": 0, "bottom": 0,
                            "onPress": [
                                {
                                    "type": "SetValue",
                                    "sequencer": "controlliToccoVideo",
                                    "componentId": "rootVideo",
                                    "property": "controlliVisibiliVideo",
                                    "value": "${!controlliVisibiliVideo}",
                                },
                                {
                                    "type": "SetValue",
                                    "sequencer": "controlliToccoVideo",
                                    "componentId": "rootVideo",
                                    "property": "controlliVisibiliVideo",
                                    "value": False,
                                    "delay": 4000,
                                    "when": "${controlliVisibiliVideo}",
                                },
                            ],
                        },
                        {
                            "type": "Container",
                            "display": "${controlliVisibiliVideo ? 'normal' : 'none'}",
                            "position": "absolute", "bottom": 0, "left": 0, "right": 0,
                            "width": "100%",
                            "alignItems": "center",
                            "paddingBottom": "24dp",
                            "items": [
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "alignItems": "center",
                                    "items": [
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "26dp", "paddingRight": "26dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": _COMANDI_INDIETRO_VIDEO,
                                            "item": {
                                                "type": "Text",
                                                "text": "‹",
                                                "fontSize": "46dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue",
                                                    "componentId": "rootVideo",
                                                    "property": "videoSilenziatoLocale",
                                                    "value": "${!videoSilenziatoLocale}",
                                                },
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "${videoSilenziatoLocale ? 'Muto' : 'Audio'}",
                                                "fontSize": "22dp", "fontWeight": "600",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            # Preferiti (24/08/2026) — stesso principio del
                                            # documento foto. Feedback istantaneo
                                            # (25/08/2026) via preferitaOverrideIndice/
                                            # Valore — vedi nota completa nel documento
                                            # foto qui sopra.
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue", "componentId": "rootVideo",
                                                    "property": "preferitaOverrideIndice",
                                                    "value": "${indiceVideoLocale}",
                                                },
                                                {
                                                    "type": "SetValue", "componentId": "rootVideo",
                                                    "property": "preferitaOverrideValore",
                                                    "value": "${!payload.videoSlideshowData.properties.video[indiceVideoLocale].preferita}",
                                                },
                                                {
                                                    "type": "SendEvent",
                                                    "arguments": [
                                                        "toggla_preferito", "video",
                                                        "${payload.videoSlideshowData.properties.video[indiceVideoLocale].nome}",
                                                        "${!payload.videoSlideshowData.properties.video[indiceVideoLocale].preferita}",
                                                    ],
                                                },
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "${(preferitaOverrideIndice == indiceVideoLocale ? preferitaOverrideValore : payload.videoSlideshowData.properties.video[indiceVideoLocale].preferita) ? '❤' : '♡'}",
                                                "fontSize": "26dp",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue",
                                                    "componentId": "rootVideo",
                                                    "property": "inPausaLocaleVideo",
                                                    "value": "${!inPausaLocaleVideo}",
                                                },
                                                {
                                                    "type": "ControlMedia",
                                                    "componentId": "videoAttuale",
                                                    "command": "pause",
                                                    "when": "${inPausaLocaleVideo}",
                                                },
                                                {
                                                    "type": "ControlMedia",
                                                    "componentId": "videoAttuale",
                                                    "command": "play",
                                                    "when": "${!inPausaLocaleVideo}",
                                                },
                                            ],
                                            "item": {
                                                "type": "VectorGraphic",
                                                "source": "${inPausaLocaleVideo ? 'iconaPlay' : 'iconaPausa'}",
                                                "width": "32dp", "height": "32dp",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "26dp", "paddingRight": "26dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": _COMANDI_AVANZA_VIDEO,
                                            "item": {
                                                "type": "Text",
                                                "text": "›",
                                                "fontSize": "46dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
            }
        ],
    },
}


# --- Documento APL per lo slideshow MISTO foto+video (Fase 2, 23/08/2026).
# Stessa filosofia del documento video-solo: NIENTE Pager (causerebbe lo
# stesso bug audio già trovato — un Pager tiene montate anche le pagine
# adiacenti, quindi se una di quelle fosse un video con autoplay partirebbe
# comunque). Un solo indice avanza su un array misto di elementi taggati
# "tipo": "foto"/"video" — a ogni cambio, il blocco giusto (foto o video)
# si mostra tramite "display" condizionale, l'altro resta nascosto.
#
# Avanzamento: per un video usa "onEnd" (fine naturale), esattamente come
# nel documento video-solo. Per una foto serve un timer — ottenuto con un
# vero "AutoPage" (stesso motore collaudato delle foto pure, screenLock
# reale) puntato su un Pager "fantasma" invisibile fatto solo di pagine
# vuote (vedi "metronomoMisto" più sotto), non su un timer scritto a mano
# (un primo tentativo con "Sequential"+"Idle" non teneva viva la sessione
# in modo affidabile — vedi cronologia bug). Il metronomo viene FERMATO
# esplicitamente mentre gira un video e fatto ripartire quando si torna
# a una foto (vedi _COMANDI_GESTISCI_METRONOMO_DOPO) — segnalato dal vivo
# il 23/08/2026 che avere DUE meccanismi con screenLock attivi insieme
# (metronomo + video) rendeva la sessione meno stabile di foto/video
# separati, che ne hanno sempre uno solo attivo alla volta.
_COMANDI_GESTISCI_METRONOMO_DOPO = [
    {
        # Appena il NUOVO elemento corrente è un video: ferma il
        # metronomo (mandare un comando qualsiasi sul suo sequencer
        # dedicato annulla l'AutoPage in corso, stesso principio già
        # usato per pausa/ripresa nel documento foto).
        "type": "Idle",
        "sequencer": "metronomoSequencer",
        "delay": 0,
        "when": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'video'}",
    },
    {
        # Appena il NUOVO elemento corrente è una foto: (ri)avvia il
        # metronomo da zero — mandare un nuovo AutoPage sullo stesso
        # sequencer annulla comunque un'istanza precedente ancora attiva,
        # quindi funziona sia per il primo avvio sia per una ripresa.
        "type": "AutoPage",
        "componentId": "metronomoMisto",
        "sequencer": "metronomoSequencer",
        "duration": "${durataSecondiLocale * 1000}",
        "count": 999,
        "screenLock": True,
        "when": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'foto'}",
    },
]

# Versione ridotta (solo "ferma", niente "riavvia") usata SOLO dal tick del
# metronomo stesso (vedi _COMANDI_TIMER_TICK_MISTO più sotto) — riavviare
# l'AutoPage da DENTRO la reazione al proprio stesso cambio pagina non è
# un caso testato altrove su questa skill: più prudente farlo scattare
# solo dai punti "esterni" (tocco, fine video), dove è già collaudato
# (stesso principio del pulsante pausa/ripresa nel documento foto). Se
# resta su una foto, l'AutoPage in corso continua da solo, non serve
# alcun intervento qui.
_COMANDI_FERMA_METRONOMO_SE_VIDEO = [
    {
        "type": "Idle",
        "sequencer": "metronomoSequencer",
        "delay": 0,
        "when": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'video'}",
    },
]

# Comandi per passare all'elemento successivo/precedente — condivisi tra
# il tocco manuale e "onEnd" del video (fine naturale). Ogni cambio
# elemento chiama anche _COMANDI_GESTISCI_METRONOMO_DOPO per fermare o
# far ripartire il metronomo secondo il tipo del nuovo elemento corrente.
# NOTA (23/08/2026): questi due comandi NON toccano più il metronomo
# (niente _COMANDI_GESTISCI_METRONOMO_DOPO qui) — segnalato dal vivo un
# blocco: dopo alcuni tap rapidi sui tasti avanti/indietro, l'avanzamento
# automatico restava bloccato su una foto (indietro funzionava ancora,
# avanti no, poi uno "scatto" di più foto insieme una volta sbloccato).
# Ipotesi: tap ravvicinati mandavano più comandi "riavvia AutoPage" in
# rapida successione sullo stesso sequencer, con una lettura dell'indice
# non sempre coerente tra un tap e l'altro. Il riavvio del metronomo resta
# SOLO sul trigger naturale e mai "a raffica" — la fine di un video
# (_COMANDI_VIDEO_ONEND_MISTO, sotto) — il tocco manuale si limita a
# cambiare foto/video senza interferire con lui.
_COMANDI_AVANZA_MISTO = [
    {
        "type": "SetValue",
        "componentId": "rootMisto",
        "property": "indiceMistoLocale",
        "value": "${indiceMistoLocale + 1}",
        "when": "${indiceMistoLocale < payload.mediaSlideshowData.properties.totaleMisto - 1}",
    },
    {"type": "SetValue", "componentId": "rootMisto", "property": "inPausaLocaleMisto", "value": False},
    {
        # Come nel documento video-solo: "autoplay" scatta solo al primo
        # montaggio, ri-bindare "source" su un componente già montato non
        # lo fa ripartire da solo — serve dirglielo esplicitamente, ma solo
        # se il NUOVO elemento corrente è davvero un video.
        "type": "ControlMedia",
        "componentId": "videoAttualeMisto",
        "command": "play",
        "when": "${indiceMistoLocale < payload.mediaSlideshowData.properties.totaleMisto - 1 && payload.mediaSlideshowData.properties.media[indiceMistoLocale + 1].tipo == 'video'}",
    },
    {
        "type": "SendEvent",
        "arguments": ["blocco_misto_esaurito"],
        "when": "${indiceMistoLocale >= payload.mediaSlideshowData.properties.totaleMisto - 1}",
    },
]

# Usata SOLO da "onEnd" del componente Video (fine naturale, un trigger
# singolo e pulito — mai soggetto a tap ravvicinati) — qui sì che ha senso
# far ripartire il metronomo, perché stiamo davvero tornando a una foto.
_COMANDI_VIDEO_ONEND_MISTO = [*_COMANDI_AVANZA_MISTO, *_COMANDI_GESTISCI_METRONOMO_DOPO]

_COMANDI_INDIETRO_MISTO = [
    {
        "type": "SetValue",
        "componentId": "rootMisto",
        "property": "indiceMistoLocale",
        "value": "${indiceMistoLocale - 1}",
        "when": "${indiceMistoLocale > 0}",
    },
    {"type": "SetValue", "componentId": "rootMisto", "property": "inPausaLocaleMisto", "value": False},
    {
        "type": "ControlMedia",
        "componentId": "videoAttualeMisto",
        "command": "play",
        "when": "${indiceMistoLocale > 0 && payload.mediaSlideshowData.properties.media[indiceMistoLocale - 1].tipo == 'video'}",
    },
]

# Tick del timer automatico (solo foto): stessa identica logica di
# _COMANDI_AVANZA_MISTO ma con la guardia aggiuntiva "solo se l'elemento
# CORRENTE (non quello nuovo) è una foto e non siamo in pausa" — se in
# quel momento c'è un video in riproduzione, questo giro non fa nulla,
# l'avanzamento del video lo gestisce "onEnd" per conto suo. Scritte per
# esteso (non composte da pezzi di stringa) per restare leggibili.
_GUARDIA_TIMER_MISTO = "payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'foto' && !inPausaLocaleMisto"
_COMANDI_TIMER_TICK_MISTO = [
    {
        "type": "SetValue",
        "componentId": "rootMisto",
        "property": "indiceMistoLocale",
        "value": "${indiceMistoLocale + 1}",
        "when": "${" + _GUARDIA_TIMER_MISTO + " && indiceMistoLocale < payload.mediaSlideshowData.properties.totaleMisto - 1}",
    },
    {
        "type": "ControlMedia",
        "componentId": "videoAttualeMisto",
        "command": "play",
        "when": "${" + _GUARDIA_TIMER_MISTO + " && indiceMistoLocale < payload.mediaSlideshowData.properties.totaleMisto - 1 && payload.mediaSlideshowData.properties.media[indiceMistoLocale + 1].tipo == 'video'}",
    },
    {
        "type": "SendEvent",
        "arguments": ["blocco_misto_esaurito"],
        "when": "${" + _GUARDIA_TIMER_MISTO + " && indiceMistoLocale >= payload.mediaSlideshowData.properties.totaleMisto - 1}",
    },
    *_COMANDI_FERMA_METRONOMO_SE_VIDEO,
]

# Timer per le foto — SECONDO tentativo, 23/08/2026 (il primo, un
# "Sequential" scritto a mano con "Idle"+delay, teneva viva la sessione
# solo ~74s/5 foto: nessun meccanismo di screenLock reale dietro). Qui
# invece si usa il VERO "AutoPage", quello già collaudato e affidabile
# per le foto pure — ma puntato su un Pager "fantasma" fatto SOLO di
# pagine vuote (vedi "metronomoMisto" più sotto), mai contenuto reale.
# Ogni cambio pagina del fantasma (evento "onPageChanged", meccanismo
# già affidabile su questa skill) fa scattare la stessa logica di
# avanzamento di prima. Zero rischio del bug audio: il Pager qui non
# contiene MAI un componente Video, solo Frame vuoti — anche se il
# runtime ne precarica pagine adiacenti, non c'è nulla da mandare in
# autoplay per sbaglio.
_COMANDI_ONMOUNT_MISTO = [
    {
        "type": "AutoPage",
        "componentId": "metronomoMisto",
        # Stesso nome di sequencer usato in _COMANDI_GESTISCI_METRONOMO_DOPO/
        # _COMANDI_FERMA_METRONOMO_SE_VIDEO per poterlo fermare/riavviare.
        "sequencer": "metronomoSequencer",
        # Placeholder, sostituito con un numero letterale da
        # _documento_misto_con_durata() — un'espressione "${...}" qui non
        # si risolve in modo affidabile (vedi nota lì).
        "duration": None,
        "count": 999,
        "screenLock": True,
    },
]

APL_DOCUMENT_MISTO = {
    "type": "APL",
    "version": "1.9",
    "graphics": GRAFICHE_ICONE,
    "onMount": _COMANDI_ONMOUNT_MISTO,
    "mainTemplate": {
        "parameters": ["payload"],
        "items": [
            {
                "type": "Frame",
                "id": "rootMisto",
                "position": "absolute",
                "top": 0, "left": 0, "right": 0, "bottom": 0,
                "backgroundColor": "#000000",
                "bind": [
                    {"name": "indiceMistoLocale", "type": "number", "value": 0},
                    {"name": "durataSecondiLocale", "type": "number", "value": "${payload.mediaSlideshowData.properties.durataSecondi}"},
                    {"name": "controlliVisibiliMisto", "type": "boolean", "value": False},
                    {"name": "inPausaLocaleMisto", "type": "boolean", "value": False},
                    # Condiviso tra foto e video, MAI resettato al cambio
                    # elemento — su richiesta di Ivan: se lo silenzi mentre
                    # è a schermo una foto, il prossimo video arriva già
                    # muto, non riparte "audio acceso" per conto suo.
                    {"name": "videoSilenziatoLocale", "type": "boolean", "value": False},
                    # Feedback istantaneo del tasto preferiti (25/08/2026) —
                    # segnalato dal vivo: il cuoricino non cambiava subito al
                    # tap perché legge lo stato da payload (si aggiorna solo
                    # al render/blocco successivo, scelta deliberata per non
                    # toccare le liste di comandi di navigazione già delicate
                    # su questo file). Fix a basso rischio: due bind SOLO per
                    # "l'ultimo tap", letti PRIMA del payload nell'icona (vedi
                    # sotto) — tornano irrilevanti da soli appena l'indice
                    # cambia (indice diverso = niente override), nessuna
                    # lista di comandi esistente da tenere sincronizzata.
                    {"name": "preferitaOverrideIndice", "type": "number", "value": -1},
                    {"name": "preferitaOverrideValore", "type": "boolean", "value": False},
                ],
                "item": {
                    "type": "Container",
                    "width": "100%",
                    "height": "100%",
                    "items": [
                        {
                            # Pager "fantasma": 1dp x 1dp, pagine vuote,
                            # usato SOLO come metronomo per l'AutoPage sopra
                            # (vedi _COMANDI_ONMOUNT_MISTO) — mai contenuto
                            # reale, quindi zero rischio del bug audio anche
                            # se il runtime precarica pagine adiacenti.
                            "type": "Pager",
                            "id": "metronomoMisto",
                            "width": "1dp",
                            "height": "1dp",
                            "position": "absolute",
                            "top": 0, "left": 0,
                            "navigation": "normal",
                            "data": "${payload.mediaSlideshowData.properties.tickMetronomo}",
                            "onPageChanged": _COMANDI_TIMER_TICK_MISTO,
                            "item": {"type": "Frame", "width": "1dp", "height": "1dp"},
                        },
                        {
                            # Blocco foto — stessa tecnica "cornice digitale"
                            # (sfondo sfocato + foto intera) già usata nel
                            # documento foto-solo. Video non supporta
                            # "filters" (niente Blur), per questo il blocco
                            # video sotto è trattato diversamente.
                            "type": "Container",
                            "width": "100%",
                            "height": "100%",
                            "display": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'foto' ? 'normal' : 'none'}",
                            "items": [
                                {
                                    "type": "Image",
                                    "source": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].url}",
                                    "position": "absolute",
                                    "width": "100%", "height": "100%",
                                    "scale": "best-fill",
                                    "align": "center",
                                    "filters": [{"type": "Blur", "radius": "30dp"}],
                                },
                                {
                                    "type": "Image",
                                    "source": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].url}",
                                    "position": "absolute",
                                    "width": "100%", "height": "100%",
                                    "scale": "best-fit",
                                    "align": "center",
                                },
                            ],
                        },
                        {
                            "type": "Video",
                            "id": "videoAttualeMisto",
                            "width": "100%",
                            "height": "100%",
                            "scale": "best-fit",
                            "autoplay": True,
                            "muted": "${videoSilenziatoLocale}",
                            "screenLock": True,
                            "display": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo == 'video' ? 'normal' : 'none'}",
                            "source": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].url}",
                            "onEnd": _COMANDI_VIDEO_ONEND_MISTO,
                        },
                        {
                            "type": "Text",
                            "position": "absolute",
                            "left": "31dp",
                            "bottom": "31dp",
                            "display": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].didascalia != '' ? 'normal' : 'none'}",
                            "text": "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].didascalia}",
                            "color": "white",
                            "fontSize": "38dp",
                            "fontWeight": "600",
                            "shadowColor": "rgba(0,0,0,0.9)",
                            "shadowOffsetX": 0,
                            "shadowOffsetY": 0,
                            "shadowRadius": 8,
                        },
                        {
                            "type": "TouchWrapper",
                            "position": "absolute",
                            "top": 0, "left": 0, "right": 0, "bottom": 0,
                            "onPress": [
                                {
                                    "type": "SetValue",
                                    "sequencer": "controlliToccoMisto",
                                    "componentId": "rootMisto",
                                    "property": "controlliVisibiliMisto",
                                    "value": "${!controlliVisibiliMisto}",
                                },
                                {
                                    "type": "SetValue",
                                    "sequencer": "controlliToccoMisto",
                                    "componentId": "rootMisto",
                                    "property": "controlliVisibiliMisto",
                                    "value": False,
                                    "delay": 4000,
                                    "when": "${controlliVisibiliMisto}",
                                },
                            ],
                        },
                        {
                            "type": "Container",
                            "display": "${controlliVisibiliMisto ? 'normal' : 'none'}",
                            "position": "absolute", "bottom": 0, "left": 0, "right": 0,
                            "width": "100%",
                            "alignItems": "center",
                            "paddingBottom": "24dp",
                            "items": [
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "alignItems": "center",
                                    "items": [
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "26dp", "paddingRight": "26dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": _COMANDI_INDIETRO_MISTO,
                                            "item": {
                                                "type": "Text",
                                                "text": "‹",
                                                "fontSize": "46dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue",
                                                    "componentId": "rootMisto",
                                                    "property": "videoSilenziatoLocale",
                                                    "value": "${!videoSilenziatoLocale}",
                                                },
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "${videoSilenziatoLocale ? 'Muto' : 'Audio'}",
                                                "fontSize": "22dp", "fontWeight": "600",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            # Preferiti (24/08/2026) — stesso principio dei
                                            # documenti foto/video, il "tipo" (foto/video)
                                            # serve alla Lambda per sapere quale endpoint
                                            # chiamare. Feedback istantaneo (25/08/2026) via
                                            # preferitaOverrideIndice/Valore — vedi nota
                                            # completa nel documento foto più sopra.
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue", "componentId": "rootMisto",
                                                    "property": "preferitaOverrideIndice",
                                                    "value": "${indiceMistoLocale}",
                                                },
                                                {
                                                    "type": "SetValue", "componentId": "rootMisto",
                                                    "property": "preferitaOverrideValore",
                                                    "value": "${!payload.mediaSlideshowData.properties.media[indiceMistoLocale].preferita}",
                                                },
                                                {
                                                    "type": "SendEvent",
                                                    "arguments": [
                                                        "toggla_preferito",
                                                        "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo}",
                                                        "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].nome}",
                                                        "${!payload.mediaSlideshowData.properties.media[indiceMistoLocale].preferita}",
                                                    ],
                                                },
                                            ],
                                            "item": {
                                                "type": "Text",
                                                "text": "${(preferitaOverrideIndice == indiceMistoLocale ? preferitaOverrideValore : payload.mediaSlideshowData.properties.media[indiceMistoLocale].preferita) ? '❤' : '♡'}",
                                                "fontSize": "26dp",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "30dp", "paddingRight": "30dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": [
                                                {
                                                    "type": "SetValue",
                                                    "componentId": "rootMisto",
                                                    "property": "inPausaLocaleMisto",
                                                    "value": "${!inPausaLocaleMisto}",
                                                },
                                                {
                                                    "type": "ControlMedia",
                                                    "componentId": "videoAttualeMisto",
                                                    "command": "pause",
                                                    "when": "${inPausaLocaleMisto}",
                                                },
                                                {
                                                    "type": "ControlMedia",
                                                    "componentId": "videoAttualeMisto",
                                                    "command": "play",
                                                    "when": "${!inPausaLocaleMisto}",
                                                },
                                            ],
                                            "item": {
                                                "type": "VectorGraphic",
                                                "source": "${inPausaLocaleMisto ? 'iconaPlay' : 'iconaPausa'}",
                                                "width": "32dp", "height": "32dp",
                                            },
                                        },
                                        {
                                            "type": "TouchWrapper",
                                            "paddingLeft": "26dp", "paddingRight": "26dp",
                                            "paddingTop": "22dp", "paddingBottom": "22dp",
                                            "onPress": _COMANDI_AVANZA_MISTO,
                                            "item": {
                                                "type": "Text",
                                                "text": "›",
                                                "fontSize": "46dp", "fontWeight": "300",
                                                "color": "rgba(255,255,255,0.85)",
                                            },
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
            }
        ],
    },
}


def _costruisci_datasource_misto(media, durata_secondi):
    return {
        "mediaSlideshowData": {
            "type": "object",
            "properties": {
                "media": [
                    {
                        "tipo": m["tipo"],
                        "nome": m["nome"],
                        "url": _url_video(m["nome"]) if m["tipo"] == "video" else _url_immagine(m["nome"]),
                        "didascalia": m.get("didascalia", ""),
                        "preferita": m.get("preferita", False),
                    }
                    for m in media
                ],
                "totaleMisto": len(media),
                "durataSecondi": durata_secondi,
                # Pagine fittizie per il Pager "fantasma"/metronomo — solo
                # numeri, mai mostrati. 200 tick coprono ~50 minuti anche
                # alla durata più lunga (300s/foto), ben oltre una sessione
                # realistica; costo trascurabile nel payload (interi piccoli).
                "tickMetronomo": list(range(200)),
            },
        }
    }


def _documento_misto_con_durata(durata_secondi):
    """Stesso principio di _documento_con_durata (foto pure): un'espressione
    "${...}" dentro "onMount" non si risolve in modo affidabile in quel
    punto del documento (bug già documentato nella storia di questo
    progetto) — scritta come numero letterale invece. Segnalato dal vivo
    il 23/08/2026: senza questo fix le foto del misto restavano a schermo
    solo ~0,5 secondi invece dei 15 configurati (il metronomo AutoPage
    avanzava usando un valore di durata non risolto)."""
    doc = copy.deepcopy(APL_DOCUMENT_MISTO)
    doc["onMount"][0]["duration"] = durata_secondi * 1000
    return doc


def _direttiva_render_misto(media, durata_secondi):
    return RenderDocumentDirective(
        token="mistoSlideshowToken",
        document=_documento_misto_con_durata(durata_secondi),
        datasources=_costruisci_datasource_misto(media, durata_secondi),
    )


def _costruisci_datasource_video(video):
    return {
        "videoSlideshowData": {
            "type": "object",
            "properties": {
                "video": [
                    {
                        "nome": v["nome"], "url": _url_video(v["nome"]), "didascalia": v.get("didascalia", ""),
                        "preferita": v.get("preferita", False),
                    }
                    for v in video
                ],
                "totaleVideo": len(video),
            },
        }
    }


def _direttiva_render_video(video):
    return RenderDocumentDirective(
        token="videoSlideshowToken",
        document=APL_DOCUMENT_VIDEO,
        datasources=_costruisci_datasource_video(video),
    )


def _documento_con_durata(durata_secondi):
    """Copia di APL_DOCUMENT con la durata dell'onMount scritta come numero
    letterale (vedi nota su _COMANDI_AUTOPLAY_ONMOUNT) — deepcopy perché
    APL_DOCUMENT è condiviso/riusato ad ogni richiesta, non va mai mutato
    in place."""
    doc = copy.deepcopy(APL_DOCUMENT)
    doc["onMount"][0]["duration"] = durata_secondi * 1000
    return doc


def _imposta_modalita(handler_input, valore):
    """Ricorda, come attributo di sessione, quale documento APL è
    attualmente a schermo (foto/video/misto) — serve ai comandi vocali
    (avanti/indietro/pausa/muto, vedi sotto) per sapere quali comandi APL
    mandare quando arriva un intent che non è un tocco/UserEvent legato a
    un documento specifico. Nessun meccanismo del genere esisteva prima
    (verificato: zero usi di session_attributes/attributes_manager in
    questo file prima del 25/08/2026)."""
    handler_input.attributes_manager.session_attributes["modalita"] = valore


def _media_accadde_oggi(impostazioni, tipo=None):
    """Foto/video "accadde oggi" (negli anni passati) da anteporre
    all'apertura dello slideshow (25/08/2026) — lista vuota se l'opzione
    "accadde_oggi_schermo" è disattivata in Impostazioni (app), quindi
    zero chiamate di rete in più quando non serve. "tipo" filtra solo
    "foto" o solo "video" (None = entrambi, per la modalità mista). MAI
    parlato: Ivan aveva già tolto una volta la versione a voce perché
    ripetitiva/invadente — qui è solo l'ordine in cui compaiono le foto,
    silenzioso."""
    if not impostazioni.get("accadde_oggi_schermo", False):
        return []
    try:
        media = _get("/api/media/accadde-oggi").get("media", [])
    except Exception:
        logger.exception("Fetch accadde oggi (media) fallito")
        return []
    if tipo:
        media = [m for m in media if m.get("tipo") == tipo]
    return media


def _antepone_accadde_oggi(blocco, accadde_oggi):
    """Mette gli elementi "accadde oggi" in testa al blocco, togliendoli
    da dove già ricadevano nell'ordine mescolato normale — altrimenti
    comparirebbero due volte nella stessa sessione."""
    if not accadde_oggi:
        return blocco
    nomi_oggi = {m["nome"] for m in accadde_oggi}
    resto = [f for f in blocco if f["nome"] not in nomi_oggi]
    return accadde_oggi + resto


def _direttiva_render(foto, durata_secondi, termine_ricerca=""):
    return RenderDocumentDirective(
        token="slideshowToken",
        document=_documento_con_durata(durata_secondi),
        datasources=_costruisci_datasource(foto, durata_secondi, termine_ricerca),
    )


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return _apri_slideshow(handler_input)


# --- Comandi vocali (Idea 5, 25/08/2026) — avanti/indietro/pausa/riprendi
# (intent Alexa integrati: AMAZON.NextIntent/PreviousIntent/PauseIntent/
# ResumeIntent) + muto/audio (intent custom, servono comunque essere
# registrati nel modello di interazione della skill perché arrivino qui).
# A differenza dei tasti a schermo, questi intent NON portano con sé quale
# documento APL è attivo — da qui _imposta_modalita/_modalita_corrente
# sopra. Ogni mappa sotto rispecchia ESATTAMENTE i comandi già usati dal
# tocco equivalente sullo schermo (stessi componentId/bind var per
# documento), per restare coerenti col comportamento già collaudato — vedi
# TouchWrapper corrispondenti nei tre documenti APL più sopra.
_TOKEN_PER_MODALITA = {
    "foto": "slideshowToken",
    "video": "videoSlideshowToken",
    "misto": "mistoSlideshowToken",
}

_COMANDI_AVANTI_VOCE = {
    "foto": [
        {"type": "Idle", "delay": 0},
        {"type": "SetPage", "componentId": "fotoPager", "position": "relative", "value": 1},
        {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": True},
    ],
    "video": _COMANDI_AVANZA_VIDEO,
    "misto": _COMANDI_AVANZA_MISTO,
}

_COMANDI_INDIETRO_VOCE = {
    "foto": [
        {"type": "Idle", "delay": 0},
        {"type": "SetPage", "componentId": "fotoPager", "position": "relative", "value": -1},
        {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": True},
    ],
    "video": _COMANDI_INDIETRO_VIDEO,
    "misto": _COMANDI_INDIETRO_MISTO,
}

# Pausa/Riprendi vocali sono espliciti (True/False), non un toggle come il
# tasto a schermo — un comando "Alexa, pausa" mentre è già in pausa deve
# restare in pausa, non ripartire per errore.
_COMANDI_PAUSA_VOCE = {
    "foto": [
        {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": True},
        # Mandare un comando qualsiasi sul sequencer principale (stesso
        # usato da "onMount"/AutoPage) annulla l'AutoPage in corso — vedi
        # nota identica sul tasto pausa a schermo.
        {"type": "Idle", "delay": 0},
    ],
    "video": [
        {"type": "SetValue", "componentId": "rootVideo", "property": "inPausaLocaleVideo", "value": True},
        {"type": "ControlMedia", "componentId": "videoAttuale", "command": "pause"},
    ],
    "misto": [
        {"type": "SetValue", "componentId": "rootMisto", "property": "inPausaLocaleMisto", "value": True},
        {"type": "ControlMedia", "componentId": "videoAttualeMisto", "command": "pause"},
    ],
}

_COMANDI_RIPRENDI_VOCE = {
    # SOLO per le foto (25/08/2026, bug segnalato dal vivo): a differenza del
    # tasto fisico a schermo, qui NON si manda anche "blocco_esaurito" — quel
    # SendEvent fa arrivare un intero documento nuovo (con un nuovo onMount
    # che avvia un SECONDO AutoPage) proprio mentre il primo AutoPage appena
    # avviato qui sotto è ancora attivo: risultato, due avanzamenti automatici
    # sovrapposti che fanno sembrare lo slideshow "velocissimo". Il tasto a
    # schermo non è stato toccato (comportamento pre-esistente, invariato) —
    # qui basta semplicemente riprendere l'AutoPage sul blocco già a schermo,
    # senza richiederne uno nuovo.
    "foto": [
        {"type": "SetValue", "componentId": "root", "property": "inPausaLocale", "value": False},
        {
            "type": "AutoPage",
            "componentId": "fotoPager",
            "duration": "${durataSecondiLocale * 1000}",
            "count": 999,
            "screenLock": True,
        },
    ],
    "video": [
        {"type": "SetValue", "componentId": "rootVideo", "property": "inPausaLocaleVideo", "value": False},
        {"type": "ControlMedia", "componentId": "videoAttuale", "command": "play"},
    ],
    "misto": [
        {"type": "SetValue", "componentId": "rootMisto", "property": "inPausaLocaleMisto", "value": False},
        {"type": "ControlMedia", "componentId": "videoAttualeMisto", "command": "play"},
    ],
}

# Muto/Audio: niente per "foto" — uno slideshow di sole immagini non ha
# audio, un "Alexa, muto" in quel momento riceve solo una risposta parlata
# (vedi _esegui_comando_vocale, ramo "comando non disponibile in questa
# modalità").
_COMANDI_MUTO_VOCE = {
    "video": [{"type": "SetValue", "componentId": "rootVideo", "property": "videoSilenziatoLocale", "value": True}],
    "misto": [{"type": "SetValue", "componentId": "rootMisto", "property": "videoSilenziatoLocale", "value": True}],
}

_COMANDI_AUDIO_VOCE = {
    "video": [{"type": "SetValue", "componentId": "rootVideo", "property": "videoSilenziatoLocale", "value": False}],
    "misto": [{"type": "SetValue", "componentId": "rootMisto", "property": "videoSilenziatoLocale", "value": False}],
}


def _comandi_preferiti_voce(nuovo_valore: bool) -> dict:
    """Aggiungi/togli dai preferiti a voce (25/08/2026) — riusa lo STESSO
    SendEvent("toggla_preferito") già gestito da APLUserEventHandler per il
    cuoricino a schermo, invece di un nuovo endpoint: l'espressione
    "${payload...[indice].nome}" viene risolta dal runtime APL con lo stato
    LIVE del documento corrente (la Lambda da sola non sa quale foto/video è
    a schermo in questo momento). A differenza del tocco (che INVERTE lo
    stato), qui il valore è sempre esplicito — "aggiungi" mette sempre
    True, "togli" sempre False, mai un toggle. Aggiorna anche
    preferitaOverrideIndice/Valore per il feedback istantaneo sul
    cuoricino, stesso principio già usato per il tocco."""
    return {
        "foto": [
            {"type": "SetValue", "componentId": "root", "property": "preferitaOverrideIndice", "value": "${paginaCorrenteLocale}"},
            {"type": "SetValue", "componentId": "root", "property": "preferitaOverrideValore", "value": nuovo_valore},
            {
                "type": "SendEvent",
                "arguments": [
                    "toggla_preferito", "foto",
                    "${payload.slideshowData.properties.foto[paginaCorrenteLocale].nome}",
                    nuovo_valore,
                ],
            },
        ],
        "video": [
            {"type": "SetValue", "componentId": "rootVideo", "property": "preferitaOverrideIndice", "value": "${indiceVideoLocale}"},
            {"type": "SetValue", "componentId": "rootVideo", "property": "preferitaOverrideValore", "value": nuovo_valore},
            {
                "type": "SendEvent",
                "arguments": [
                    "toggla_preferito", "video",
                    "${payload.videoSlideshowData.properties.video[indiceVideoLocale].nome}",
                    nuovo_valore,
                ],
            },
        ],
        "misto": [
            {"type": "SetValue", "componentId": "rootMisto", "property": "preferitaOverrideIndice", "value": "${indiceMistoLocale}"},
            {"type": "SetValue", "componentId": "rootMisto", "property": "preferitaOverrideValore", "value": nuovo_valore},
            {
                "type": "SendEvent",
                "arguments": [
                    "toggla_preferito",
                    "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].tipo}",
                    "${payload.mediaSlideshowData.properties.media[indiceMistoLocale].nome}",
                    nuovo_valore,
                ],
            },
        ],
    }


_COMANDI_AGGIUNGI_PREFERITI_VOCE = _comandi_preferiti_voce(True)
_COMANDI_TOGLI_PREFERITI_VOCE = _comandi_preferiti_voce(False)


def _esegui_comando_vocale(handler_input, mappa_comandi, speak_non_disponibile=None):
    """Costruisce ed emette la direttiva ExecuteCommands giusta in base alla
    modalità corrente in sessione (vedi _imposta_modalita). Se non c'è
    ancora nessuno slideshow aperto, o la modalità corrente non supporta
    questo comando (es. muto su una foto), risponde solo a voce senza
    toccare lo schermo."""
    builder = handler_input.response_builder
    modalita = handler_input.attributes_manager.session_attributes.get("modalita")
    if not modalita:
        speak = "Apri prima le foto o i video, poi puoi usare questo comando."
        builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    comandi = mappa_comandi.get(modalita)
    if not comandi:
        speak = speak_non_disponibile or "Questo comando non è disponibile adesso."
        builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    builder.add_directive(ExecuteCommandsDirective(token=_TOKEN_PER_MODALITA[modalita], commands=comandi))
    builder.set_should_end_session(False)
    return builder.response


class MostraGalleriaIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("MostraGalleriaIntent")(handler_input)

    def handle(self, handler_input):
        return _apri_slideshow(handler_input)


def _apri_slideshow(handler_input):
    if not _con_apl(handler_input):
        speak = "Questo dispositivo non ha uno schermo per mostrare le foto."
        return handler_input.response_builder.speak(speak).response

    try:
        risposta = _get("/api/foto/batch", {"n": DIMENSIONE_BLOCCO})
        blocco = risposta["foto"]
        impostazioni = _get("/api/impostazioni")
    except Exception:
        logger.exception("Fetch iniziale fallito")
        speak = "Non riesco a recuperare le foto adesso, riprova tra poco."
        return handler_input.response_builder.speak(speak).response

    if not blocco:
        speak = "Non ci sono ancora foto caricate."
        return handler_input.response_builder.speak(speak).response

    blocco = _antepone_accadde_oggi(blocco, _media_accadde_oggi(impostazioni, tipo="foto"))
    durata = impostazioni.get("durata_secondi", 10)
    builder = handler_input.response_builder.speak("Ecco le tue foto.")
    builder.add_directive(_direttiva_render(blocco, durata))
    _imposta_modalita(handler_input, "foto")
    # L'avvio dell'autoplay parte da solo via "onMount" dentro il documento,
    # non serve più una direttiva ExecuteCommands separata (vedi nota su
    # _COMANDI_AUTOPLAY_ONMOUNT).
    # Come nel Freezer: True chiuderebbe anche lo schermo, non solo il
    # microfono — qui serve restare aperti indefinitamente (è una
    # cornice digitale), quindi sempre False.
    builder.set_should_end_session(False)
    return builder.response


class MostraVideoIntentHandler(AbstractRequestHandler):
    """"Mostra i video" — Fase 1: apre il Pager video separato (vedi
    APL_DOCUMENT_VIDEO), non tocca in alcun modo il flusso foto."""
    def can_handle(self, handler_input):
        return is_intent_name("MostraVideoIntent")(handler_input)

    def handle(self, handler_input):
        return _apri_video(handler_input)


def _apri_video(handler_input):
    if not _con_apl(handler_input):
        speak = "Questo dispositivo non ha uno schermo per mostrare i video."
        return handler_input.response_builder.speak(speak).response

    try:
        risposta = _get("/api/video/batch", {"n": DIMENSIONE_BLOCCO_VIDEO})
        blocco = risposta["video"]
        impostazioni = _get("/api/impostazioni")
    except Exception:
        logger.exception("Fetch video iniziale fallito")
        speak = "Non riesco a recuperare i video adesso, riprova tra poco."
        return handler_input.response_builder.speak(speak).response

    if not blocco:
        speak = "Non ci sono ancora video caricati."
        return handler_input.response_builder.speak(speak).response

    blocco = _antepone_accadde_oggi(blocco, _media_accadde_oggi(impostazioni, tipo="video"))
    builder = handler_input.response_builder.speak("Ecco i tuoi video.")
    builder.add_directive(_direttiva_render_video(blocco))
    _imposta_modalita(handler_input, "video")
    builder.set_should_end_session(False)
    return builder.response


class MostraFotoVideoIntentHandler(AbstractRequestHandler):
    """"Mostra foto e video" — Fase 2: slideshow misto (vedi
    APL_DOCUMENT_MISTO), terza "isola" separata da foto-sole e video-soli."""
    def can_handle(self, handler_input):
        return is_intent_name("MostraFotoVideoIntent")(handler_input)

    def handle(self, handler_input):
        return _apri_foto_video(handler_input)


def _apri_foto_video(handler_input):
    if not _con_apl(handler_input):
        speak = "Questo dispositivo non ha uno schermo per mostrare foto e video."
        return handler_input.response_builder.speak(speak).response

    try:
        risposta = _get("/api/media/batch", {"n": DIMENSIONE_BLOCCO_MISTO})
        blocco = risposta["media"]
        impostazioni = _get("/api/impostazioni")
    except Exception:
        logger.exception("Fetch misto iniziale fallito")
        speak = "Non riesco a recuperare foto e video adesso, riprova tra poco."
        return handler_input.response_builder.speak(speak).response

    if not blocco:
        speak = "Non ci sono ancora foto o video caricati."
        return handler_input.response_builder.speak(speak).response

    blocco = _antepone_accadde_oggi(blocco, _media_accadde_oggi(impostazioni))
    durata = impostazioni.get("durata_secondi", 10)
    builder = handler_input.response_builder.speak("Ecco foto e video.")
    builder.add_directive(_direttiva_render_misto(blocco, durata))
    _imposta_modalita(handler_input, "misto")
    builder.set_should_end_session(False)
    return builder.response


class MostraArgomentoIntentHandler(AbstractRequestHandler):
    """"Mostra dicembre" (qualsiasi anno) o "mostra Villaspeciosa" — stesso
    comando vocale, il backend (vedi server.py, handle_cerca) decide da solo
    se il termine è uno dei 12 mesi o un luogo. Vista "usa e getta": non
    tocca il cursore della galleria principale, che resta dov'era."""
    def can_handle(self, handler_input):
        return is_intent_name("MostraArgomentoIntent")(handler_input)

    # Lo slot AMAZON.SearchQuery di "mostra {argomento}" è volutamente molto
    # ampio, e cattura ANCHE le frasi pensate per la galleria normale — "mostra
    # foto" combacia perfettamente col pattern con argomento="foto", quindi
    # Alexa lo instrada qui invece che a MostraGalleriaIntent (bug segnalato
    # dal vivo il 23/08/2026: comando "non trovato" e sessione chiusa, perché
    # la ricerca per un luogo/mese chiamato "foto" non trova nulla). Fix: se
    # l'argomento catturato è una delle parole generiche della galleria,
    # dirotta su _apri_slideshow invece di trattarlo come un termine di
    # ricerca — indipendentemente da quale intent l'NLU abbia scelto.
    _PAROLE_GALLERIA = {
        "foto", "le foto", "tutte le foto", "tutte", "galleria", "la galleria",
        "le mie foto", "tutte le mie foto",
    }
    # Stesso identico problema, stessa soluzione: "mostra i video" combacia
    # anche lui col pattern "mostra {argomento}" (Fase 1 video, 23/08/2026).
    _PAROLE_VIDEO = {
        "video", "i video", "tutti i video", "i miei video", "tutti i miei video",
    }
    _PAROLE_MISTO = {
        "foto e video", "foto e i video", "le foto e i video", "le foto e video",
        "foto video", "video e foto", "video e le foto",
    }

    # Ricerca nei video/nel misto (24/08/2026) — stesso principio già in uso
    # per _PAROLE_VIDEO/_PAROLE_MISTO sopra: lo slot AMAZON.SearchQuery è
    # aperto e cattura per intero frasi come "mostra i video di dicembre
    # 2023" (argomento = "i video di dicembre 2023", il "mostra" iniziale
    # non fa parte dello slot). Si riconosce il prefisso e si toglie prima
    # di passare il resto come termine di ricerca. MISTO va controllato
    # PRIMA di VIDEO — "foto e video di ..." contiene la parola "video" ma
    # deve finire nella ricerca mista, non in quella solo-video.
    _PREFISSI_RICERCA_MISTO = (
        "foto e video di ", "le foto e i video di ", "le foto e video di ",
        "foto e i video di ", "video e foto di ", "video e le foto di ",
        "foto video di ",
    )
    _PREFISSI_RICERCA_VIDEO = (
        "i video di ", "video di ", "i miei video di ", "tutti i video di ",
    )

    def handle(self, handler_input):
        if not _con_apl(handler_input):
            speak = "Questo dispositivo non ha uno schermo per mostrare le foto."
            return handler_input.response_builder.speak(speak).response

        slots = handler_input.request_envelope.request.intent.slots
        argomento = (slots["argomento"].value or "").strip() if slots and "argomento" in slots else ""
        if not argomento:
            speak = "Non ho capito cosa cercare, puoi ripetere?"
            return handler_input.response_builder.speak(speak).ask(speak).response

        argomento_norm = argomento.lower()
        if argomento_norm in self._PAROLE_GALLERIA:
            return _apri_slideshow(handler_input)
        if argomento_norm in self._PAROLE_VIDEO:
            return _apri_video(handler_input)
        if argomento_norm in self._PAROLE_MISTO:
            return _apri_foto_video(handler_input)

        for prefisso in self._PREFISSI_RICERCA_MISTO:
            if argomento_norm.startswith(prefisso):
                return _apri_ricerca_misto(handler_input, argomento[len(prefisso):].strip())
        for prefisso in self._PREFISSI_RICERCA_VIDEO:
            if argomento_norm.startswith(prefisso):
                return _apri_ricerca_video(handler_input, argomento[len(prefisso):].strip())

        try:
            risposta = _get("/api/foto/cerca", {"q": argomento})
            impostazioni = _get("/api/impostazioni")
        except Exception:
            logger.exception("Ricerca fallita")
            speak = "Non riesco a cercare le foto adesso, riprova tra poco."
            builder = handler_input.response_builder.speak(speak)
            builder.set_should_end_session(False)
            return builder.response

        blocco = risposta.get("foto", [])
        totale = risposta.get("totale_trovate", 0)

        if not blocco:
            speak = f"Non ho trovato nessuna foto per {argomento}."
            builder = handler_input.response_builder.speak(speak)
            builder.set_should_end_session(False)
            return builder.response

        if totale > len(blocco):
            speak = f"Ne ho trovate {totale}, te ne mostro {len(blocco)}."
        else:
            speak = f"Ecco {totale} foto." if totale != 1 else "Ecco una foto."

        durata = impostazioni.get("durata_secondi", 10)
        builder = handler_input.response_builder.speak(speak)
        builder.add_directive(_direttiva_render(blocco, durata, argomento))
        _imposta_modalita(handler_input, "foto")
        builder.set_should_end_session(False)
        return builder.response


def _apri_ricerca_video(handler_input, termine):
    """Ricerca nei video ("mostra i video di dicembre 2023") — stesso
    principio della ricerca foto, ma sul blocco video-solo (24/08/2026).
    NOTA: a differenza della ricerca foto, qui non c'è ancora un meccanismo
    di continuazione oltre il primo blocco (nessun "termineRicerca" portato
    dentro APL_DOCUMENT_VIDEO) — se i risultati superano DIMENSIONE_BLOCCO_VIDEO
    si vede solo il primo blocco, poi lo slideshow torna alla galleria video
    normale. Scelta deliberata per non toccare ulteriormente le liste di
    comandi del documento video (già delicate, vedi cronologia bug) senza
    prima capire se serve davvero dal vivo."""
    if not termine:
        speak = "Non ho capito cosa cercare nei video, puoi ripetere?"
        return handler_input.response_builder.speak(speak).ask(speak).response

    try:
        risposta = _get("/api/video/cerca", {"q": termine})
    except Exception:
        logger.exception("Ricerca video fallita")
        speak = "Non riesco a cercare i video adesso, riprova tra poco."
        builder = handler_input.response_builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    blocco = risposta.get("video", [])
    totale = risposta.get("totale_trovate", 0)

    if not blocco:
        speak = f"Non ho trovato nessun video per {termine}."
        builder = handler_input.response_builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    if totale > len(blocco):
        speak = f"Ne ho trovati {totale}, te ne mostro {len(blocco)}."
    else:
        speak = f"Ecco {totale} video." if totale != 1 else "Ecco un video."

    builder = handler_input.response_builder.speak(speak)
    builder.add_directive(_direttiva_render_video(blocco))
    _imposta_modalita(handler_input, "video")
    builder.set_should_end_session(False)
    return builder.response


def _apri_ricerca_misto(handler_input, termine):
    """Ricerca su foto e video insieme ("mostra le foto e i video di dicembre
    2023") — stessa nota di _apri_ricerca_video sulla continuazione oltre il
    primo blocco, non ancora collegata."""
    if not termine:
        speak = "Non ho capito cosa cercare, puoi ripetere?"
        return handler_input.response_builder.speak(speak).ask(speak).response

    try:
        risposta = _get("/api/media/cerca", {"q": termine})
        impostazioni = _get("/api/impostazioni")
    except Exception:
        logger.exception("Ricerca mista fallita")
        speak = "Non riesco a cercare adesso, riprova tra poco."
        builder = handler_input.response_builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    blocco = risposta.get("media", [])
    totale = risposta.get("totale_trovate", 0)

    if not blocco:
        speak = f"Non ho trovato nulla per {termine}."
        builder = handler_input.response_builder.speak(speak)
        builder.set_should_end_session(False)
        return builder.response

    if totale > len(blocco):
        speak = f"Ne ho trovati {totale}, te ne mostro {len(blocco)}."
    else:
        speak = f"Ecco {totale} risultati." if totale != 1 else "Ecco un risultato."

    durata = impostazioni.get("durata_secondi", 10)
    builder = handler_input.response_builder.speak(speak)
    builder.add_directive(_direttiva_render_misto(blocco, durata))
    _imposta_modalita(handler_input, "misto")
    builder.set_should_end_session(False)
    return builder.response


class APLUserEventHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("Alexa.Presentation.APL.UserEvent")(handler_input)

    def handle(self, handler_input):
        request = handler_input.request_envelope.request
        args = getattr(request, "arguments", None) or []
        kind = args[0] if args else None

        builder = handler_input.response_builder

        if kind == "blocco_esaurito":
            termine_ricerca = (args[1] if len(args) >= 2 else "") or ""
            try:
                if termine_ricerca:
                    # Sto continuando una ricerca (es. "mostra dicembre") oltre
                    # il primo blocco da DIMENSIONE_BLOCCO — "continua": "1" dice
                    # al server di riprendere dal cursore salvato invece di
                    # rimescolare daccapo (vedi server.py, handle_cerca), così
                    # ogni chiamata pesca foto NUOVE, non le stesse ripetute.
                    blocco = _get("/api/foto/cerca", {"q": termine_ricerca, "continua": "1"})["foto"]
                else:
                    blocco = _get("/api/foto/batch", {"n": DIMENSIONE_BLOCCO})["foto"]
                impostazioni = _get("/api/impostazioni")
            except Exception:
                logger.exception("Fetch blocco successivo fallito")
                builder.set_should_end_session(False)
                return builder.response
            if not blocco and termine_ricerca:
                # Ricerca esaurita davvero: nessuna foto nuova da mostrare,
                # torno silenziosamente alla galleria normale invece di
                # bloccare lo slideshow su un blocco vuoto.
                try:
                    blocco = _get("/api/foto/batch", {"n": DIMENSIONE_BLOCCO})["foto"]
                except Exception:
                    logger.exception("Fetch galleria di fallback fallito")
                    builder.set_should_end_session(False)
                    return builder.response
                termine_ricerca = ""
            durata = impostazioni.get("durata_secondi", 10)
            builder.add_directive(_direttiva_render(blocco, durata, termine_ricerca))
            _imposta_modalita(handler_input, "foto")

        elif kind == "blocco_video_esaurito":
            # Il video Pager (vedi APL_DOCUMENT_VIDEO) ha finito i video di
            # questo blocco — pesca il prossimo blocco dal cursore mescolato
            # persistente (stato_shuffle_video.json), stesso principio delle
            # foto ma stato completamente separato.
            try:
                blocco = _get("/api/video/batch", {"n": DIMENSIONE_BLOCCO_VIDEO})["video"]
            except Exception:
                logger.exception("Fetch blocco video successivo fallito")
                builder.set_should_end_session(False)
                return builder.response
            if not blocco:
                builder.set_should_end_session(False)
                return builder.response
            builder.add_directive(_direttiva_render_video(blocco))
            _imposta_modalita(handler_input, "video")

        elif kind == "blocco_misto_esaurito":
            # Stesso principio di "blocco_video_esaurito" — pesca il
            # prossimo blocco misto (server.py, handle_media_batch).
            try:
                blocco = _get("/api/media/batch", {"n": DIMENSIONE_BLOCCO_MISTO})["media"]
                impostazioni = _get("/api/impostazioni")
            except Exception:
                logger.exception("Fetch blocco misto successivo fallito")
                builder.set_should_end_session(False)
                return builder.response
            if not blocco:
                builder.set_should_end_session(False)
                return builder.response
            durata = impostazioni.get("durata_secondi", 10)
            builder.add_directive(_direttiva_render_misto(blocco, durata))
            _imposta_modalita(handler_input, "misto")

        elif kind == "toggla_preferito":
            # Tasto ❤️ sullo schermo (24/08/2026) — args: ["toggla_preferito",
            # tipo ("foto"/"video"), nome, nuovo_valore (bool)]. Nessuna
            # direttiva di ritorno: il cuoricino sullo schermo resta legato
            # al valore ricevuto nel blocco corrente (payload...[indice].
            # preferita) e si aggiorna da solo al prossimo blocco/render —
            # scelta deliberata per non dover tenere sincronizzato un bind
            # locale su ogni comando di navigazione esistente (rischio di
            # bug analogo a quelli già trovati stanotte toccando quei punti).
            tipo = args[1] if len(args) >= 2 else None
            nome = args[2] if len(args) >= 3 else None
            nuovo_valore = args[3] if len(args) >= 4 else None
            if tipo in ("foto", "video") and nome and nuovo_valore is not None:
                percorso = "/api/foto" if tipo == "foto" else "/api/video"
                try:
                    _post(f"{percorso}/{urllib.parse.quote(nome)}/preferita", {"preferita": bool(nuovo_valore)})
                except Exception:
                    logger.exception("Impostazione preferito fallita")

        builder.set_should_end_session(False)
        return builder.response


class AvantiIntentHandler(AbstractRequestHandler):
    """AMAZON.NextIntent ("Alexa, avanti"/"prossima") — stesso identico
    effetto della freccia "›" a schermo, applicato alla modalità corrente
    (vedi _COMANDI_AVANTI_VOCE/_esegui_comando_vocale)."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NextIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_AVANTI_VOCE)


class IndietroIntentHandler(AbstractRequestHandler):
    """AMAZON.PreviousIntent ("Alexa, indietro"/"precedente")."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PreviousIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_INDIETRO_VOCE)


class PausaIntentHandler(AbstractRequestHandler):
    """AMAZON.PauseIntent ("Alexa, pausa"/"fermati") — a differenza del
    tasto a schermo (che inverte lo stato), qui il comando è esplicito:
    mette sempre in pausa, non la toglie se è già in pausa."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PauseIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_PAUSA_VOCE)


class RiprendiIntentHandler(AbstractRequestHandler):
    """AMAZON.ResumeIntent ("Alexa, riprendi"/"continua")."""
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_RIPRENDI_VOCE)


class MutoIntentHandler(AbstractRequestHandler):
    """Intent custom "MutoIntent" ("Alexa, muto"/"silenzia") — richiesto da
    Ivan il 24/08/2026 in aggiunta ad avanti/indietro/pausa. Non esiste per
    le foto (nessun audio): in quel caso risponde solo a voce, vedi
    _esegui_comando_vocale."""
    def can_handle(self, handler_input):
        return is_intent_name("MutoIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(
            handler_input, _COMANDI_MUTO_VOCE,
            speak_non_disponibile="Le foto non hanno audio da silenziare.",
        )


class AudioIntentHandler(AbstractRequestHandler):
    """Intent custom "AudioIntent" ("Alexa, audio"/"riattiva l'audio")."""
    def can_handle(self, handler_input):
        return is_intent_name("AudioIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(
            handler_input, _COMANDI_AUDIO_VOCE,
            speak_non_disponibile="Le foto non hanno audio da riattivare.",
        )


class AggiungiPreferitiIntentHandler(AbstractRequestHandler):
    """Intent custom "AggiungiPreferitiIntent" ("Alexa, aggiungi a
    preferiti") — richiesto da Ivan il 25/08/2026."""
    def can_handle(self, handler_input):
        return is_intent_name("AggiungiPreferitiIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_AGGIUNGI_PREFERITI_VOCE)


class TogliPreferitiIntentHandler(AbstractRequestHandler):
    """Intent custom "TogliPreferitiIntent" ("Alexa, togli dai
    preferiti") — complemento simmetrico di AggiungiPreferitiIntent."""
    def can_handle(self, handler_input):
        return is_intent_name("TogliPreferitiIntent")(handler_input)

    def handle(self, handler_input):
        return _esegui_comando_vocale(handler_input, _COMANDI_TOGLI_PREFERITI_VOCE)


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speak = "Dì mostrami le foto per avviare lo slideshow. Poi puoi toccare lo schermo per i controlli."
        return handler_input.response_builder.speak(speak).ask(speak).response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.CancelIntent")(handler_input) or \
            is_intent_name("AMAZON.StopIntent")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.speak("A presto!").response


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        speak = "Non ho capito, puoi dire mostrami le foto."
        return handler_input.response_builder.speak(speak).ask(speak).response


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        # Prima non veniva loggato nulla — quando la sessione si chiudeva da
        # sola senza un errore Python visibile (segnalato dal vivo il
        # 23/08/2026, chiusura ~3 secondi dopo l'apertura) non c'era modo di
        # sapere il motivo. "reason" può essere USER_INITIATED, ERROR,
        # EXCEEDED_MAX_REPROMPTS — se ERROR, "error" ha il dettaglio.
        request = handler_input.request_envelope.request
        reason = getattr(request, "reason", None)
        error = getattr(request, "error", None)
        logger.info(f"SessionEndedRequest — reason={reason} error={error}")
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error(exception, exc_info=True)
        speak = "C'è stato un problema, riprova."
        return handler_input.response_builder.speak(speak).ask(speak).response


class LogRequestInterceptor(AbstractRequestInterceptor):
    """Logga tipo/intent di OGNI richiesta — aggiunto il 23/08/2026 per
    diagnosticare chiusure premature della sessione (segnalate dal vivo,
    reason=USER_INITIATED senza che l'utente avesse detto/toccato nulla):
    prima si vedeva solo l'eventuale SessionEndedRequest finale, senza sapere
    cosa fosse successo PRIMA (un blocco_esaurito partito ma mai arrivato a
    buon fine? nessuna richiesta affatto, segno che è il player client-side
    a fermarsi da solo?)."""
    def process(self, handler_input):
        request = handler_input.request_envelope.request
        tipo = request.object_type if hasattr(request, "object_type") else type(request).__name__
        extra = ""
        if tipo == "IntentRequest":
            extra = f" intent={request.intent.name}"
        elif tipo == "Alexa.Presentation.APL.UserEvent":
            extra = f" arguments={getattr(request, 'arguments', None)}"
        logger.info(f"Richiesta ricevuta: {tipo}{extra}")


sb = SkillBuilder()
sb.add_global_request_interceptor(LogRequestInterceptor())
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(MostraGalleriaIntentHandler())
sb.add_request_handler(MostraVideoIntentHandler())
sb.add_request_handler(MostraFotoVideoIntentHandler())
sb.add_request_handler(MostraArgomentoIntentHandler())
sb.add_request_handler(APLUserEventHandler())
sb.add_request_handler(AvantiIntentHandler())
sb.add_request_handler(IndietroIntentHandler())
sb.add_request_handler(PausaIntentHandler())
sb.add_request_handler(RiprendiIntentHandler())
sb.add_request_handler(MutoIntentHandler())
sb.add_request_handler(AudioIntentHandler())
sb.add_request_handler(AggiungiPreferitiIntentHandler())
sb.add_request_handler(TogliPreferitiIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
