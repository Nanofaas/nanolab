# NanoLab: workflow soak per P24 su una singola versione

Data: 2026-09-13
Stato: progetto approvato in conversazione; specifica scritta da sottoporre a revisione prima del piano di implementazione.
Owner: NanoLab per orchestrazione, infrastruttura, raccolta e valutazione. NanoFaaS per il comportamento del sistema osservato.

## 1. Obiettivo e riferimento

Realizzare un workflow permanente NanoLab per eseguire il protocollo P24 su una singola versione di NanoFaaS. Il riferimento e' `docs/plans/2026-09-08-control-plane-lifecycle-memory-and-modularity.md`, sezione P24, nel checkout NanoFaaS.

Lo strumento deve dimostrare che nessuna popolazione priva di ownership cresce con la durata o con la storia dei nomi, e che dopo drain rimane solo stato utile entro limiti e retention documentati. Un plateau di payload di lavoro concluso trattenuto per una finestra lunga non diventa accettabile perche' smette di crescere.

Il workflow non confronta revisioni, non gestisce bracci baseline/candidate e non dichiara chiusa l'intera campagna. L'eventuale confronto storico del piano P24 si effettua separatamente usando artefatti di esecuzioni indipendenti. Il riferimento iniziale di memoria descritto sotto appartiene allo stesso processo e alla stessa esecuzione, non a un'altra versione.

## 2. Perimetro

Prima implementazione: backend container locale; control plane, funzioni Java JVM e JavaScript dello scenario storico; supporto al profilo native solo tramite capacita' diagnostiche esplicite e realmente disponibili. Un profilo che richiede osservazioni non supportate viene rifiutato prima del carico lungo, non trattato come misurato.

Il percorso principale usa invocazioni SYNC senza chiavi, carico moderato e costante, circa 90 minuti di carico piu' drain. Warm-up, diagnosi e drain non consumano i 90 minuti. Autoscaling e variazioni automatiche dei limiti sono disabilitati per questo scenario.

Il workflow supporta scenari brevi separati per i prerequisiti pertinenti P24, inclusi errori/cancellazioni, replay/idempotenza, asincrono e churn. Non li aggrega al workload storico. Il soak SYNC non prova da solo proprieta' esercitate soltanto dal churn o dall'asincrono.

Non obiettivi: nuovo motore di workflow, comparatore di build, provisioning nei test Java, supporto Kubernetes dichiarato senza implementazione, tuning automatico per ottenere un verde, esecuzione automatica del soak lungo nella normale CI.

## 3. Integrazione architetturale

Introdurre un workflow dichiarativo `soak`, composto mediante Sonata. Riutilizzare le primitive NanoLab esistenti per ambiente, immagini, funzioni, k6 e gestione risorse. Non creare un orchestratore shell alternativo e non accumulare ulteriori rami specifici in `plans/loadtest.py`.

Punti di partenza esistenti: gli scenari `memory-soak-sync-container.yaml` e `memory-soak-sync-native.yaml`, il task di drain in `tasks/loadtest/soak.py` e l'integrazione loadtest. Migrare i preset soak al nuovo workflow; preservare gli altri scenari loadtest e documentare la migrazione dei vecchi campi soak.

Responsabilita' separate:

| Unita' | Contratto |
| --- | --- |
| Configurazione | Validare protocollo, workload, capacita' richieste e criteri prima del run |
| Preparazione | Possedere l'ambiente e risolvere gli artefatti immutabili |
| Osservatore | Produrre campioni per processo, con timestamp, unita', origine e disponibilita' |
| Diagnostica | Acquisire checkpoint compatibili con il runtime, indicando costo e completamento |
| Valutatore | Leggere artefatti senza interrogare o modificare il sistema sotto test |
| Report | Esporre esiti, copertura, violazioni e attribuzioni necessarie |

Le sonde dipendono dal backend/runtime, non il protocollo. Nessuna nuova libreria condivisa e' necessaria per questa prima implementazione.

## 4. Contratto dello scenario e provenienza

La configurazione risolta contiene immagini/digest di control plane e funzioni, moduli, runtime, GC e opzioni heap, CPU/RAM, numero di funzioni e repliche, payload e relativo hash, flag warm, rate, concorrenza, timeout, retry, retention effettive, calendario delle fasi e politiche di accettazione.

Il manifest registra identita' NanoLab, NanoFaaS e script del generatore; identifica separatamente sorgenti modificati e artefatti costruiti da essi. Non attribuire a un semplice commit una build con modifiche locali. I tag sono risolti a digest e si verifica l'identita' dei container effettivamente avviati; nessuna ricostruzione silenziosa dopo il congelamento degli artefatti.

Le soglie richieste devono essere presenti prima del run. Il valutatore non deriva tolleranze dai risultati osservati e non amplia limiti dopo un fallimento. Le politiche dichiarano popolazione, metrica/unita', limite, deadline, finestra di valutazione e giustificazione. Ogni processo ha budget di memoria e regole di rientro o di residuo ammesso esplicite.

## 5. Protocollo di esecuzione

1. Validazione statica e rappresentazione tramite `nanolab plan`, senza provisioning.
2. Preparazione e preflight: avvio ambiente, identita' artefatti, configurazione effettiva, limiti cgroup realmente applicati, raggiungibilita', diagnostica, spazio disponibile e capacita' del generatore. Salvare dichiarato e osservato. Un limite assente o diverso invalida il protocollo prima del carico lungo.
3. Warm-up a durata massima dichiarata, seguito da drain del warm-up. Acquisire il riferimento a riposo con le funzioni ancora presenti. Salvare anche l'avvio a freddo. Se non si raggiungono le condizioni iniziali dichiarate, non proseguire indefinitamente.
4. Carico costante con osservazione continua. Registrare offered, admitted quando osservabile, successi, errori, retry, replay e richieste non erogate dal generatore. Non inventare un conteggio admitted quando manca la fonte.
5. Arresto del generatore e drain naturale, senza riavvio dei processi, cancellazione delle funzioni o pulizia manuale delle cache. Acquisire checkpoint rispetto all'istante effettivo di arresto del traffico.
6. Checkpoint diagnostici finali e valutazione prima del teardown.
7. Pubblicazione degli artefatti e cleanup delle sole risorse possedute dal run.

Il calendario e' calcolato dalle retention effettive: durata di carico sufficiente per almeno tre cicli della finestra piu' lunga pertinente; drain oltre tale finestra e il margine di cleanup dichiarato. Con finestre di 30 secondi, 5 minuti e 30 minuti, il preset storico usa 90 minuti di carico e 35 minuti di drain. Una configurazione con finestre maggiori richiede durate maggiori; non mantenere ciecamente i preset.

I checkpoint comprendono riferimento iniziale, fine carico, attraversamento delle finestre pertinenti con margine e fine drain. Gli appuntamenti usano deadline monotone assolute: il tempo di scraping non si accumula come deriva nascosta. Il report distingue istante previsto, inizio/fine reale e campioni mancati.

I prerequisiti brevi sono eseguiti prima del soak oppure forniti mediante ricevute verificabili con artefatti e configurazioni pertinenti coincidenti. Ogni scenario dichiara la copertura richiesta; ricevute mancanti o incompatibili impediscono un PASS P24. La semplice riuscita di uno smoke non sostituisce tali evidenze.

## 6. Osservazione e diagnosi della memoria

Misurare separatamente control plane, proxy se presente e ogni processo SDK, mantenendo identita' di processo/container e ruolo. Un riavvio non deve azzerare silenziosamente la serie: e' un evento che invalida la continuita' del soak.

Raccogliere RSS/PSS quando accessibili, composizione cgroup, heap usato/committed e post-GC verificato, GC log, buffer diretti, thread, socket, pool HTTP e pending, code refresh/timer, esecuzioni vive, outcome, chiavi, byte stimati, owner ritirati e cardinalita' delle metriche.

Non sommare heap, RSS e cgroup. Non sommare label che distinguono categorie necessarie all'attribuzione, per esempio pool heap/non-heap. Conservare le identita' di serie per rilevare crescita delle label, non soltanto nuovi nomi di metriche. La cardinalita' del registry e quella dell'esposizione Prometheus sono osservazioni distinte: non chiamare la seconda una misura esatta della prima.

Il manifesto delle capacita' distingue `observed`, `unavailable` e `not_applicable`, con motivo. Una metrica obbligatoria assente non equivale a zero. Il profilo native non simula metriche JVM.

Gli osservatori restano attivi durante carico e drain. Buffer in memoria limitati e persistenza incrementale impediscono che la strumentazione stessa accumuli tutti i campioni in RAM. Dati grezzi ed eventuali aggregati restano distinguibili.

I checkpoint post-GC sono validi solo se la raccolta e' stata osservata, non semplicemente richiesta. GC forzato e dump vengono marcati come finestre diagnostiche, fuori dalle misure ordinarie di latenza. Il rientro naturale delle risorse si valuta prima di interventi diagnostici che possano alterarlo. Non forzare eviction, riavvio o cancellazioni amministrative per far passare il drain.

Crescita post-GC, popolazioni oltre deadline o plateau sospetti attivano raccolta diagnostica limitata per numero, durata e spazio: istogrammi, dump/root analysis e strumenti runtime compatibili. Gli istogrammi da soli non dimostrano l'ownership dei retainers. L'attribuzione indica popolazione, owner, eta' attesa, motivo della permanenza e riferimento agli artefatti; quando non automatizzabile resta una valutazione esplicita da completare.

## 7. Criteri di accettazione

Il successo HTTP e la stabilita' di RSS non bastano. Verificare correttezza, validita' del carico, completezza delle osservazioni e regole di retention/budget per tutti i ruoli richiesti.

Le popolazioni transitorie devono rientrare nei valori ammessi entro le deadline. Lo stato utile residuo deve avere owner, limite e retention documentati. La crescita con la durata o la storia dei nomi va valutata nei profili che esercitano tali dimensioni.

Non esiste un'esenzione generica per memoria attribuita al runtime. Ogni residuo deve rispettare il budget dichiarato e la politica dello scenario. Se e' richiesto il ritorno RSS al riferimento, dichiarare riferimento, tolleranza e deadline: un plateau superiore non soddisfa tale criterio. Se e' ammesso un residuo, questo deve essere limitato e giustificato, non soltanto etichettato come JVM o allocator.

Un RSS residuo sospetto non attribuito impedisce PASS anche quando le metriche applicative rientrano. Viceversa, il solo RSS elevato non autorizza a diagnosticare automaticamente un leak di oggetti Java.

| Esito | Regola |
| --- | --- |
| PASS | Protocollo completato, prerequisiti e osservazioni obbligatorie validi, criteri soddisfatti e nessuna attribuzione richiesta rimasta aperta |
| FAIL | Violazione dimostrata su osservazioni valide, inclusi budget, retention, correttezza o riavvii non ammessi |
| INCONCLUSIVE | Nessuna conclusione completa possibile: preflight fallito, carico insufficiente, osservazioni mancanti o residuo sospetto non attribuito |
| ABORTED | Interruzione esterna prima del completamento del protocollo |

Il report mantiene verdetti per criterio: una violazione gia' dimostrata non scompare se il run viene poi interrotto o perde una sonda. A protocollo concluso, una violazione dimostrata produce FAIL anche con altri criteri inconcludenti. PASS richiede tutti i criteri obbligatori soddisfatti. Ogni esito diverso da PASS produce exit code non zero, con motivo strutturato; gli errori di strumentazione sono distinti da quelli del sistema osservato.

PASS si riferisce al profilo e alla durata eseguiti, non dimostra l'assenza universale di leak e non chiude automaticamente P24 o il confronto storico.

## 8. Artefatti, cancellazione e risorse

Ogni run ha un identificatore e una directory dedicati, fuori dagli scratchpad temporanei. Conservare configurazione risolta, manifest, ricevute dei prerequisiti, eventi/fasi, serie grezze, dati k6, log, diagnostica, risultati JSON versionati e report leggibile. I file voluminosi hanno dimensione e checksum registrati.

Salvare incrementalmente eventi e campioni. Il report finale espone componenti non osservati, campioni persi, effetto delle finestre diagnostiche e motivi di invalidita'. Deve essere rivalutabile dai soli artefatti; una nuova analisi non sovrascrive il verdetto precedente senza conservarne la provenienza.

Su errore o cancellazione: fermare il generatore, scaricare i buffer, tentare la raccolta finale entro un timeout finito, scrivere lo stato parziale, quindi chiudere le risorse possedute. Non attendere automaticamente 35 minuti di drain dopo una cancellazione. Errori di cleanup restano visibili senza mascherare la causa primaria.

Integrare il meccanismo NanoLab/Sonata esistente di conservazione esplicita dell'ambiente; non distruggere risorse preesistenti o di altri run. Non cancellare gli artefatti con il teardown. Non sovrapporre run su risorse condivise senza isolamento verificabile.

Un run interrotto non puo' essere ripreso e presentato come un soak continuo: la nuova esecuzione ha un nuovo identificatore. E' invece consentita la rivalutazione offline delle evidenze gia' raccolte.

## 9. Verifica e consegna

Test unitari con clock, sonde e diagnostica simulati: calendario/deadline, parser e label, metriche assenti, precedenza degli esiti, budget e retention, GC non osservato, artefatti parziali, cancellazione e cleanup.

Test di composizione Sonata: ordine delle fasi, osservatore attivo fino a fine drain, nessun teardown prima della raccolta, isolamento del generatore e nessuna mutazione del sistema per soddisfare il gate.

Test del valutatore con fixture positive e violazioni controllate: crescita monotona, plateau di payload conclusi, rientro corretto, violazione RSS nonostante heap rientrato, carico perso, riavvio e cardinalita' crescente.

Smoke reale breve per provare il workflow e la persistenza, esplicitamente classificato come smoke e non come accettazione P24. Il primo run P24 lungo viene avviato solo dopo le verifiche mirate, con immagini ricostruite contenenti i fix e identita' congelate.

Consegna: workflow e preset mantenuti in NanoLab, documentazione di esecuzione e lettura degli esiti, test, esempio di report e collegamenti alle evidenze P24 in NanoFaaS. Le modifiche a codice/configurazione avvengono dopo il piano di implementazione e l'analisi di impatto richiesta dal repository; questa specifica non modifica codice ne' avvia esperimenti.

## 10. Chiarimento approvato: build delle immagini nel workflow

Il percorso normale deve compilare tutte le immagini NanoFaaS necessarie dai sorgenti selezionati: control plane, funzioni/SDK e proxy di progetto se previsto. Non richiede che l'operatore prepari immagini con tag convenzionali prima di lanciare il workflow. Questa sezione prevale sulle indicazioni che presuppongono immagini applicative precompilate.

La preparazione diventa: controlli statici e prerequisiti di build, snapshot identificato dei sorgenti, build delle varianti richieste, pubblicazione nel registry del run, congelamento dei digest, deploy e preflight runtime. Soltanto dopo iniziano le fasi di misura. Il registry e gli strumenti esterni mantengono immagini/toolchain identificati; non e' necessario ricompilare prodotti terzi.

Ogni componente dichiara variante e opzioni. Il control plane JVM e quello native hanno ricette distinte; una funzione Java native non puo' essere ricompilata implicitamente come JVM. JavaScript include l'SDK costruito dagli stessi sorgenti selezionati. Le ricette e i moduli effettivi fanno parte del manifest. La fase di build usa le primitive NanoLab esistenti, non un secondo sistema di build.

Le modifiche locali sono ammesse, ma lo snapshot deve identificarle e restare stabile durante tutte le build. Registrare hash degli input, toolchain e immagini base effettivamente usate, comandi, log, durata, piattaforma e digest di output. Non dedurre la provenienza di un'immagine da un tag o dalla sola revisione Git del checkout.

Usare tag univoci per run durante la pubblicazione e digest immutabili per il deploy; nessuna ricompilazione dopo il congelamento o durante il soak. Un errore di build interrompe la preparazione con evidenze e senza avviare il carico; non effettua fallback a immagini precedenti.

Una modalita' esplicita `prebuilt` puo' evitare la build per un componente quando richiesta dall'operatore, ma richiede digest e provenienza e non e' il default del preset P24. Il normale cache reuse della build e' ammesso con gli stessi input identificati; una vecchia immagine applicativa trovata per tag non costituisce cache valida.

`nanolab plan` mostra le build previste senza eseguirle. La misura non include consumo e tempi di compilazione: builder terminati, attivita' di build esaurita e condizioni iniziali ripristinate prima del warm-up/riferimento. Conservare le immagini base/cache secondo ownership e policy, senza pulizie globali di Docker.
