# Esperimento 01 nanoFaaS — piano di implementazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** estendere `compare` per misurare working set e CPU del control plane k3s verso una sola `qr-code-rust`, con sette configurazioni mirate e tre ripetizioni interlacciate.

**Architecture:** conservare provisioning, prepare/pubblicazione, distribuzioni immutabili, matrice, directory, resume, raccolta e report di compare. Aggiungere un profilo di carico dedicato e contratti specifici dell’esperimento; mantenere separati composizione e runtime nell’identità, pur usando sette chiavi di configurazione nella matrice esistente. L’aggiornamento Azure è una modifica preliminare indipendente.

**Tech Stack:** Python 3.12, Pydantic, Sonata, azure-vm-sdk 0.7.0, recipe nanoFaaS/Gradle, Helm/k3s, k6, Prometheus/cAdvisor, pandas/Plotly già presenti.

**Spec:** prompt dell’utente di questa sessione, «primo esperimento del paper nanoFaaS». I vincoli e criteri di accettazione sono riportati sotto; nessuna verifica del paper esterno è stata eseguita.

## Ambito della consegna corrente

Questo documento è il risultato della richiesta «analizza questo prompt e scrivi il piano», aggiornato dopo la revisione su esiti/resume, validità del generatore, fine dei gradini k6, ordine delle ripetizioni e prerequisiti Sonata. La consegna corrente aggiorna soltanto il piano. Nessun provisioning, installazione, build nativa o carico è stato avviato. I nomi nuovi descritti qui sono proposte da implementare e verificare prima di documentarli come disponibili.

## Riscontri nel checkout

- NanoLab: `packages/nanolab/pyproject.toml` richiede `azure-vm-sdk==0.6.3`, `sonata-engine==0.6.15` e `sonata-tasks[azure,...]==0.6.15`; il workspace e `uv.lock` concordano.
- Il checkout vicino `../sonata/packages/sonata-tasks/pyproject.toml` impone ancora `azure-vm-sdk>=0.6.2,<0.7`. Anche `../azure-vm-sdk` dichiara 0.6.3: non costituisce prova delle API 0.7.0.
- Il provider da conservare è `sonata_tasks/vm/providers/azure.py`: importa `AzureClient`, `AzureVM`, `VmNotFoundError`; usa costruzione del client, `get_vm`, `ensure_running`, `launch`, handle VM e comandi Azure CLI per fatti/rete. Va censito integralmente, inclusi SSH, trasferimenti e teardown.
- `comparison/profiles.py` forza il profilo `jvm` condiviso e l’insieme esatto `{k8s-deployment-provider, async-queue, build-metadata}`. Il manifest forza anche l’identità dello scheduler unificato. Questi contratti sono storici, non universali.
- CLI e schema richiedono due funzioni; `plans/runtime_comparison.py` accede a `functions[1]`, imposta coda 20 e sceglie gli script storici.
- `comparison/matrix.py` ripete lo stesso ordine delle varianti e considera completa una cella solo con summary k6 e snapshot Prometheus. `cli/comparison.py` ritenta qualsiasi eccezione nella stessa directory: questi comportamenti richiedono un contratto dedicato per gli esiti dell’esperimento.
- `images/control_plane_variants.py` mantiene `jvm` a C1; `jvm_optimization()` distingue soltanto `c1`/`c2`. `jvm-c2` è già full tiering, ma il suo nome non deve diventare una descrizione «solo C2».
- Il packaging include già `assets/k6/*`, recipe, scenari e payload. Occorre provarlo attraverso wheel installata, non aggiungere copie.
- Il catalogo ha working set cAdvisor e CPU con `rate(...[30s])`; l’adapter Prometheus usa un passo predefinito di 5 secondi.
- `tasks/validation/function_contracts.py` verifica struttura e dimensioni PNG: questo non dimostra da solo il testo codificato nel QR.
- `one_shot/recording.py` contiene un recorder legato agli artifact della campagna one-shot; `tasks/one_shot/logging.py` porta lifecycle e backend di logging non necessari qui. Conservare compare e il suo heartbeat; riusare solo primitive davvero neutrali, senza importare questi sottosistemi.
- Nessun `AGENTS.md` trovato nel percorso sorgente di NanoLab, Sonata o SDK nella ricerca effettuata. Letto `../nanofaas/AGENTS.md`: prima di eventuali modifiche nanoFaaS sono obbligatorie le analisi GitNexus richieste. Rileggere le istruzioni applicabili quando si aprono altri checkout.
- Checkout NanoLab su `main`; esistono file non tracciati estranei in `docs/reports/` e alcuni binari/script nella root. Non includerli né modificarli.

## Vincoli globali

- Solo Azure, VM `stack` e `loadgen`, k3s; niente arm-builder, endpoint esterno o backend sperimentale alternativo.
- Un solo orchestratore: compare. Niente calibratore, prodotto cartesiano, framework generico, dashboard distinta, nuove dipendenze se quelle presenti bastano, refactoring estraneo.
- Tutte le configurazioni includono core implicito e provider Kubernetes; baseline senza altri moduli opzionali. Nessun `build-metadata` aggiunto per superare verifiche.
- Serial GC e massimo heap control plane **64 MiB** in ogni runtime; limiti container di CPU e memoria identici e distinti dall’heap. Mai aumentare risorse per nascondere avvio fallito/OOM.
- Una sola `qr-code-rust`, un solo digest costruito/pubblicato in prepare; payload, runtime, risorse, timeout, concorrenza e una replica pronta comuni a tutti i run.
- Payload iniziale: testo ASCII deterministico di 1024 byte e `size: 1024`; FIXED inizialmente 2; coda 200 solo nella composizione che la include. Nessuno di questi è un asse della matrice.
- Autoscaling, governor adattivo, offload e P2P disabilitati. Readiness e identità restano obbligatorie; sovraccarico atteso non è un fallimento di qualifica.
- Niente build/pubblicazioni durante il carico. JIT consentito durante warmup e misura. Nessuna GC forzata fra gradini.
- Preservare recipe, default, semantica di varianti e comportamento dei confronti storici, compreso `jvm` = C1.
- La successiva implementazione, quando richiesta, comprende modifiche e verifiche locali. Provisioning Azure, compilazioni native lunghe e campagne restano fuori ambito fino a successiva esecuzione autorizzata.

## Matrice fissata

Le chiavi di configurazione proposte coincidono con i basename delle recipe. La classificazione runtime è un campo distinto; le due composizioni dello stesso runtime non devono collassare nello stesso artifact o risultato.

| Recipe in `assets/presets/recipes/` | Moduli opzionali, oltre core | Runtime |
| --- | --- | --- |
| `experiment-01-core-k8s-jvm-jit.yaml` | k8s-deployment-provider | JVM full tiering C1→C2 |
| `experiment-01-core-k8s-native-o3.yaml` | k8s-deployment-provider | Native O3 |
| `experiment-01-core-k8s-queue-jvm-int.yaml` | k8s-deployment-provider, async-queue | JVM -Xint |
| `experiment-01-core-k8s-queue-jvm-c1.yaml` | k8s-deployment-provider, async-queue | JVM C1 |
| `experiment-01-core-k8s-queue-jvm-jit.yaml` | k8s-deployment-provider, async-queue | JVM full tiering C1→C2 |
| `experiment-01-core-k8s-queue-native-o2.yaml` | k8s-deployment-provider, async-queue | Native O2 |
| `experiment-01-core-k8s-queue-native-o3.yaml` | k8s-deployment-provider, async-queue | Native O3 |

Tre ripetizioni = 21 celle interlacciate, con un ordine diverso per ripetizione solo per `experiment-01`. Indicando con A–G le sette righe della tabella, gli ordini sono `A B C D E F G`, `D G B F A E C`, `F C E A G D B`. Ogni configurazione occupa tre posizioni diverse; questo riduce il confondimento con posizione e predecessore, ma non costituisce un controbilanciamento completo. Il pilot a una ripetizione usa il primo ordine; eventuali ripetizioni ulteriori ripetono il ciclo di tre ordini. Se si seleziona un sottoinsieme per diagnosi, filtrare questi ordini sulle chiavi selezionate e non presentarlo come campagna completa. Conservare l’ordine storico per gli altri profili. L’ordine effettivo entra nell’identità e viene conservato al resume.

Confronti: cinque runtime con coda; composizioni a JVM JIT completo e Native O3. Il disegno **non misura tutte le interazioni runtime × composizione**. Differenze di ammissione/accodamento impediscono di attribuire tutta la differenza al costo di caricamento del modulo.

## Review focus

1. Manifest storico valido deve continuare a riprendere; identità nuova incompleta o mutata deve essere rifiutata.
2. Risposta a cavallo di due gradini conserva la coorte d’invio e non viene cancellata dal default di `gracefulStop`; un’interruzione del generatore non cancella richieste già inviate.
3. Serie mancanti, pod vecchi e baseline senza coda non diventano zeri o metriche contaminate.
4. Flag richiesti presenti nella recipe non dimostrano flag effettivi; distinguere osservato, inferito e non verificabile.
5. OOM e avvio fallito sono esiti persistenti conservati al resume, senza retry distruttivi; generatore insufficiente rende il gradino non qualificabile al rate richiesto anche con successo del 100% delle inviate.

## Task 1 — compatibilità Azure, modifica separata

**File:** root `pyproject.toml`, `packages/nanolab/pyproject.toml`, `uv.lock`; provider e `tests/vm/test_azure_provider.py` in Sonata solo se necessario; test consumer `packages/nanolab/tests/cli/test_vm_provider.py` e `tests/tasks/provisioning/`.

**Interfaccia:** conservare `AzureVmProvider` e `VmRequest`; il risultato consegnabile è un ambiente risolvibile con SDK esattamente 0.7.0 e una coppia Sonata engine/tasks della stessa versione pubblicata su PyPI, senza override che nascondano metadata incompatibili. La compatibilità provata con artifact locali è un risultato intermedio distinto.

- [ ] Procurare sorgenti/metadata esatti di SDK 0.7.0 e della release Sonata candidata, in staging locale; verificare le istruzioni dei repository. Non assumere che i checkout vicini siano aggiornati.
- [ ] Elencare ogni chiamata SDK del provider e confrontare firme, modelli, ritorni, eccezioni e semantica lifecycle/SSH con 0.7.0. Registrare le differenze reali.
- [ ] Aggiungere test di contratto senza rete: client e handle autospeccati sulle API 0.7.0 reali, esecutori CLI/SSH simulati. Coprire creazione e riuso, IP/fatti, rete limitata ai CIDR, trasferimenti, close/delete, workspace mancante e risorsa ancora presente. Impedire l’esecuzione reale di `az`, tofu e SSH.
- [ ] Preferire una Sonata già compatibile e verificata. Se non disponibile, adattare solo il provider esistente e il suo vincolo. Per verifiche locali usare una coppia engine/tasks coerente di wheel versionati con hash o sorgente fissata riproducibile in un ambiente temporaneo; conservare identità e procedura. Non introdurre path locali, URL Git o override nel lock consegnato. Non pubblicare pacchetti e non limitarsi ad allargare `<0.7`.
- [ ] Allineare dipendenze Sonata root/package, SDK e lock solo dopo che la coppia Sonata compatibile è pubblicata, secondo `CLAUDE.md`; verificare risoluzione e installazione pulite dai pin consegnati e `uv lock --check`. Se la release manca, registrare «compatibilità locale verificata; integrazione in attesa della release Sonata», mantenere invariati pin/lock consegnabili e lasciare incompleto questo gate. Proseguire con le verifiche indipendenti; non dichiarare completo il Task 1 o l’intera consegna.
- [ ] Eseguire i test provider Sonata e consumer NanoLab; consegnare questo diff/commit logico separato dall’esperimento. Nessun contatto Azure.

## Task 2 — fattibilità delle composizioni, runtime e QR

**File da verificare:** recipe/schema e toolchain nanoFaaS effettivamente referenziati da `tasks/recipes/`; `comparison/evidence.py`, `profiles.py`; `images/control_plane_variants.py`; contratti QR esistenti. Modifiche nanoFaaS soltanto se indispensabili e dopo GitNexus impact.

**Interfaccia:** prima di fissare recipe, produrre un inventario verificato: chiave configurazione → moduli effettivi, funzionalità, ammissione, runtime richiesto e prove disponibili.

- [ ] Tracciare dipendenze reali di core/provider/async-queue e il percorso di invocazione gestita. Verificare che entrambe le composizioni possano avviare e invocare senza moduli aggiunti. Se incompatibili, documentare il vincolo prima di qualsiasi sostituzione: non alterare la matrice unilateralmente.
- [ ] Verificare passaggio recipe → build → entrypoint → processo. JVM: Serial, `-Xmx64m`; interprete `-Xint`, C1 `-XX:TieredStopAtLevel=1`, JIT completo tiering fino a 4. Native: Serial, heap runtime 64 MiB tramite l’entrypoint effettivamente supportato, compilazione O2/O3 esplicita.
- [ ] Verificare supporto O2/O3 nella distribuzione/versione GraalVM scelta mediante documentazione/tool help e test di comando con builder finto. Non assumere supporto da un enum dello schema, non ricompilare nativamente per questa verifica locale e non effettuare fallback.
- [ ] Individuare come osservare argv, immagine/digest, limiti cgroup, moduli, configurazione FIXED e coda senza build-metadata; usare il manifest di build e le osservazioni Kubernetes già disponibili. Registrare separatamente prove assenti, inferenze e mismatch invalidanti.
- [ ] Verificare default reali e accessibilità del percorso con coda. Individuare una prova pilot di utilizzo della coda, e l’ammissione reale nella baseline, inclusi retry e timeout interni.
- [ ] Individuare un decoder QR già disponibile nell’ambiente/tooling per il preflight: controllare dimensioni **e testo decodificato esatto**. Il validatore PNG esistente copre solo parte della prova. Se manca una soluzione compatibile con i vincoli sulle dipendenze, documentare il blocco prima di dichiarare il preflight completo.

## Task 3 — contratto scenario, sette recipe e discovery

**File:** `config/scenario.py`; nuovo `config/experiment_01.py` per i soli parametri del protocollo; `comparison/profiles.py`; `images/control_plane_variants.py`; sette recipe nella directory canonica; `assets/presets/recipes/README.md`; nuovi scenari `experiment-01-pilot.yaml`, `experiment-01.yaml` e payload in `assets/presets/scenarios/payloads/`.

**Test:** `tests/config/test_scenario.py`, `tests/comparison/test_profiles.py`, `tests/images/test_control_plane_variants.py`, `tests/test_package_layout.py`, test discovery/workspace già presenti.

**Interfaccia proposta:** `loadProfile: experiment-01`, blocco `experiment01` tipizzato e validato in `ScenarioConfig`; registro esplicito `EXPERIMENT_01_PROFILES` con le sette chiavi sopra. Nessun generatore di recipe o registro generico di campagne.

- [ ] Scrivere test che rifiutano funzioni diverse da `['qr-code-rust']`, backend diverso da k8s, funzionalità adattive, dati non finiti, rate/durate non positivi, rate non crescenti, timeout/drain incoerenti e protocollo incompleto per la campagna.
- [ ] Definire `experiment01` con payload/path e hash risolto, idle, warmup comune, elenco rate/durata, timeout, drain, FIXED, coda per composizione, limiti VU/generatore, soglia latenza, frazione minima entro soglia, criterio pendenti/coda e stato pilot/congelato. Nessun parametro diventa asse della matrice. Distinguere input richiesti da osservazioni runtime.
- [ ] Aggiungere al protocollo tolleranza relativa massima sul rate inviato, frazione massima di `dropped_iterations`, ampiezza delle finestre di verifica e massimo ritardo di avvio rispetto al calendario. Richiedere valori finiti, frazioni in `[0, 1)`, finestra positiva non superiore al gradino e ritardo non negativo. Congelare le soglie nel pilot; la campagna le richiede esplicite. Rendere espliciti anche `gracefulStop` e margine finito per completamento/logging: per ogni scenario di carico `gracefulStop >= timeout HTTP + margine` e `drain >= gracefulStop` massimo. Testare rifiuto di combinazioni incoerenti.
- [ ] Mantenere i default storici e le due funzioni obbligatorie per `comparison`/`mixed`; applicare il nuovo contratto solo a `experiment-01`. Azure e due ruoli sono validati dalla CLI compare per questo esperimento.
- [ ] Aggiungere le sette configurazioni dedicate senza modificare tuple/default storici. Separare classificazione runtime (`interpreted`, `c1`, `tiered`, `native-o2`, `native-o3`) dalla chiave configurazione e correggere i consumatori che etichettano tutto come C1/C2.
- [ ] Scrivere manualmente le sette recipe; core resta implicito se così richiede lo schema. Solo la prima recipe pubblicata dichiara la funzione condivisa, seguendo il meccanismo esistente. Nessun profilo `jvm` storico preparato in aggiunta.
- [ ] Generare una volta e conservare il testo iniziale deterministico (per esempio ASCII `A` ripetuto 1024 volte), `size: 1024` e i byte JSON canonici effettivamente inviati. Congelare risorse identiche esplicite; non confondere heap e memoria container.
- [ ] Testare insiemi moduli esatti, flag, un solo artifact funzione, assenza funzionalità vietate e discovery. Costruire wheel e verificarla installata in ambiente temporaneo fuori dal checkout: sette recipe, script, scenario e payload devono risolversi con le API esistenti.

## Task 4 — prepare, matrice, identità e resume

**File:** `cli/comparison.py`, `comparison/{prepare,profiles,matrix,manifest,target,evidence}.py`, relativi test `tests/comparison/` e `tests/cli/test_comparison.py`.

**Interfaccia:** sette chiavi utilizzabili dall’attuale `--variants`; tre ripetizioni predefinite per l’esperimento. `ComparisonCell` e layout dei run restano il meccanismo di esecuzione; composizione e runtime entrano nell’identità dichiarata del profilo.

- [ ] Implementare e testare i tre ordini dichiarati nella matrice: 21 coppie configurazione/ripetizione uniche, tre posizioni diverse per configurazione, nessuna baseline aggiuntiva e nessuna espansione cartesiana. Testare pilot, filtro diagnostico e invarianza dell’ordine storico; salvare politica e sequenza effettiva nell’identità. Il resume filtra le celle concluse senza riordinare quelle rimaste; una modifica dell’ordine richiede una nuova directory.
- [ ] Consentire la funzione Rust singola solo per il profilo nuovo. Conservare prepare una volta e `publishRecipe`, che già costruisce: non anteporre `assembleRecipe`. Le sei distribuzioni successive riusano il digest funzione della prima.
- [ ] Derivare moduli/verifiche dal profilo esatto: baseline non deve ereditare i controlli di async-queue o build-metadata. Manifest, digest registry, distribuzione e identità pod restano obbligatori.
- [ ] Estendere il manifest con contratto esplicito per esperimento 01; leggere i manifest storici con le regole precedenti, senza migrazione silenziosa. Non applicare alla baseline l’identità fissa dello scheduler storico.
- [ ] Identificare scenario normalizzato, byte/hash payload, script, protocollo completo, soglie/criteri, moduli/funzionalità/ammissione, opzioni richieste, toolchain, artifact, risorse e host. Salvare prove osservate e fatti delle due VM: SKU, architettura, OS, risorse e identità cluster/VM.
- [ ] Testare rifiuto resume dopo qualsiasi modifica determinante, VM/cluster sostituiti, digest o prove mancanti; ripresa valida senza rebuild e senza rieseguire celle completate; `--fresh` conserva tutti i controlli.
- [ ] Aggiungere solo per l’esperimento una ricevuta atomica `cell-result.json` nella directory della cella, con identità, tentativo, esito, motivo, timestamp e riferimenti/hash delle prove. Gli esiti definitivi sono `measured`, `startup_failed`, `oom`; `measured` indica raccolta completata, non sostenibilità. Per `measured` richiedere summary, ledger e snapshot con eventuali lacune esplicite; per i fallimenti richiedere prove diagnostiche e indicazione degli artifact non producibili, senza inventare summary o serie. `completed()` riconosce gli esiti definitivi validi; conserva la regola storica per gli altri profili. Arresti infrastrutturali o interruzioni restano incompleti.
- [ ] Conservare ogni tentativo sotto `<configurazione>/run-<n>/attempts/<numero>/`, mantenendo la directory esterna della cella. Ricevuta e report selezionano il tentativo esplicitamente, senza mescolarne gli artifact; includere il tentativo nell’identità degli eventi. OOM e avvio fallito non hanno retry automatici. Consentire al massimo un retry automatico per un errore di trasporto riconosciuto prima del carico, dopo verifica identità/infrastruttura e assenza di processi/pod residui. Errori dopo l’avvio del carico o non classificati fermano l’esecuzione con prove parziali; al resume una cella incompleta riparte da un nuovo tentativo e deployment pulito. `--fresh` crea nuovi tentativi e conserva ricevute/prove precedenti.
- [ ] Proseguire dopo un esito definitivo di avvio fallito/OOM solo se identità e infrastruttura restano valide. Testare `OOM → resume → cella successiva`, avvio fallito senza summary, successo con generatore insufficiente senza retry, errore di trasporto prima/dopo il carico, interruzione prima della ricevuta atomica e `--fresh` senza sovrascritture. Il report deve leggere anche celle con soli artifact diagnostici.

## Task 5 — protocollo a plateaux e invii/completamenti

**File:** nuovo `assets/k6/experiment-01.js`; `plans/runtime_comparison.py`; adapter k6 e task loadtest effettivamente usati; test `tests/plans/test_runtime_comparison.py`, `tests/tasks/loadtest/` e smoke script dedicato.

**Interfaccia:** `script_for()` e `comparison_k6_environment()` scelgono il nuovo script e passano un unico protocollo serializzato, URL, funzione e payload. Nessun accesso a `functions[1]` nel percorso singolo. Output standard k6 più eventi minimi in artifact già raccolti.

- [ ] Testare idle → warmup → gradini a rate costante con cambi netti → drain limitato, usando executor `constant-arrival-rate` k6 con `startTime` espliciti. Allineare l’origine temporale effettiva, non quella del lancio SSH. Nessuna rampa storica riutilizzata.
- [ ] Impostare esplicitamente `gracefulStop` per warmup e ogni gradino secondo il contratto del Task 3: alla fine della durata cessano i nuovi invii, mentre le iterazioni già avviate possono terminare durante il gradino successivo. Il default k6 è 30 s e può interrompere iterazioni ancora attive ([documentazione](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/graceful-stop/)); verificarne il comportamento sulla versione utilizzata. Il drain globale parte alla fine degli ultimi invii, resta osservato per tutta la durata congelata e include le iterazioni residue. Un arresto forzato produce censura esplicita, mai un timeout HTTP inventato.
- [ ] Calcolare il budget VU complessivo includendo gli scenari contemporaneamente attivi e quelli in `gracefulStop`; registrare preallocazione, massimi e utilizzo effettivo. Non stimare il fabbisogno dal solo gradino corrente. La sovrapposizione non deve cambiare il rate dei nuovi invii; verificarlo nello smoke e nel pilot.
- [ ] Fare readiness e preflight fuori dalla misura; tenere vivi control plane e unica replica fra gradini; deployment pulito per ogni cella/tentativo e attesa assenza dei vecchi pod. Nessuna build durante idle/warmup/misura.
- [ ] Confrontare le opzioni di output già disponibili: riusare JSON k6 e metriche custom a cardinalità limitata per gradino. Aggiungere eventi JSONL con ID richiesta unico, fase/gradino, timestamp immediatamente prima di `http.post`, completamento, durata, stato, correttezza, timeout/errore; niente corpo PNG né ID richiesta come label Prometheus.
- [ ] Provare con la versione k6 effettiva che gli eventi d’invio siano emessi prima del blocco HTTP e conservati anche se il processo è interrotto. Se stdout è bufferizzato, adattare la raccolta esistente per drenarlo durante l’esecuzione; non affidarsi al solo summary o a contatori aggiornati dopo la risposta.
- [ ] Conservare il gradino catturato prima dell’invio anche se la risposta arriva dopo il confine. Non derivarlo dal timestamp di emissione finale di `http_req_duration`.
- [ ] Preflight completo QR; nel carico controllo leggero della risposta nanoFaaS effettiva: envelope, successo, media type/encoding e struttura/dimensioni PNG sufficienti secondo il contratto verificato. Documentare cosa prova il controllo leggero; nessuna decodifica completa per richiesta.
- [ ] Registrare offerte teoriche dal calendario, inviate, dropped_iterations, scostamenti e limiti VU; niente retry client nascosti. Eventi di completamento e timeout sono terminali distinti; inviate senza evento terminale alla chiusura restano pendenti/censurate, non rimosse.
- [ ] Separare timeout del client, timeout server e richieste server ancora pendenti quando osservabili; completare HTTP client non prova che il backend abbia smesso di lavorare. Drain include raccolta finale con limite esplicito.
- [ ] Salvare timestamp e confini effettivi delle fasi e prove di sincronizzazione/scostamento orologi fra generatore e stack. Riutilizzare lifecycle e heartbeat compare; niente clock/topologie/supervisione one-shot importati.
- [ ] Testare risposta nel gradino successivo, invio senza completamento, timeout, SIGTERM/troncamento log, dropped iterations e generatore in ritardo. Lo smoke locale usa server finto con risposta di contratto e rate minimi; è un test del protocollo, non un backend di misura alternativo.
- [ ] Aggiungere uno smoke mirato con risposta dopo oltre 30 s dal confine del gradino precedente ma prima del timeout configurato: deve conservare la coorte, completarsi senza cancellazione al confine ed essere raccolta nel drain. Coprire separatamente il timeout HTTP e l’interruzione forzata del generatore; verificare il rate del nuovo gradino mentre il precedente termina.

## Task 6 — raccolta e attribuzione delle risorse

**File:** `metrics/catalogue.py`, task snapshot e modelli loadtest esistenti, `plans/runtime_comparison.py`; test catalogo, risorse, snapshot window.

**Interfaccia:** snapshot Prometheus esistente con identità pod/container del run, finestra comprendente idle e drain, metadati risoluzione/scrape/rate window; artifact eventi della task 5 per coorti.

- [ ] Raccogliere working set, CPU in core, throttling, OOM e restart del control plane; stesse risorse per funzione e contesto servizi di supporto. Distinguere control plane, funzione e totale deployment senza sommare aggregati sovrapposti.
- [ ] Filtrare usando identità del pod corrente e intervalli di esistenza, non solo namespace/container. Testare residui di due run nello stesso namespace e restart durante un gradino.
- [ ] Raccogliere profondità coda solo quando il modulo esiste; assenza prevista = non applicabile, serie richiesta mancante = evidenza incompleta. Baseline registra comportamento reale di ammissione.
- [ ] Persistire passo query (attualmente 5 s), scrape effettivo e finestra CPU (attualmente 30 s). Escludere dai riassunti interni dei plateaux i punti CPU la cui finestra attraversa un confine; mostrare comunque la serie temporale. Gradino troppo breve = statistica non disponibile, non precisione inventata.
- [ ] Baseline principale: idle misurato prima del warmup, dichiarato esplicitamente. Riportare assoluto e delta rispetto a quel periodo; un eventuale idle finale è una fase distinta.
- [ ] Usare risorse container come misura comune. RSS/heap soltanto se già disponibili con semantica verificata, senza collector dedicati. Conservare eventuali assenze/limiti delle osservazioni.

## Task 7 — aggregazione e report compare

**File:** `tasks/loadtest/comparison_report.py`, helper report già presenti e test corrispondenti. Se la ricostruzione eventi richiede logica separata, un solo modulo specifico `comparison/experiment_01.py`, senza astrazioni di campagne.

**Interfaccia proposta:** `summarize_experiment_01_cell(run_dir: Path) -> dict[str, object]`, consumato dal writer compare solo per l’esperimento; conserva anche riepilogo macchina JSON/CSV accanto agli artifact esistenti.

- [ ] Ricostruire ledger per ID richiesta: invio unico, zero/un completamento; rilevare duplicati, eventi incompleti e identità incoerenti. Calcolare successo entro soglia = risposte corrette e tempestive / **tutte le inviate della coorte**, incluse quelle ancora pendenti.
- [ ] Tabella per configurazione, ripetizione e gradino: rate offerto/inviato, completamenti e throughput utile, correttezza entro soglia, p50/p95/p99 con popolazione esplicita, rifiuti/errori/timeout, pendenti, queue trend, working set/CPU assoluti e delta idle, throttling/OOM/restart.
- [ ] Distinguere risultati per coorte d’invio dai completamenti per intervallo temporale. Mostrare denominatori e richieste offerte ma non inviate come limiti del generatore, non rifiuti nanoFaaS.
- [ ] Prima della qualifica di sostenibilità, verificare la fedeltà del generatore con le soglie congelate nel Task 3: scostamento assoluto relativo fra invii osservati e previsti sull’intero gradino e sulle finestre dichiarate, `dropped_iterations / iterazioni previste` e ritardo dell’inizio effettivo del gradino rispetto al calendario. Usare la durata effettiva delle finestre parziali finali; mantenere l’origine del calendario anche quando il generatore è in ritardo. Metriche mancanti, esecuzione troncata o violazione di una soglia rendono il gradino `non qualificabile per limite del generatore`, conservando tutte le misure osservate. Non attribuire al sistema il rate richiesto quando non è stato erogato.
- [ ] Solo per gradini con carico verificato e prove richieste sufficienti applicare i criteri manualmente congelati: latenza, quota corretta entro soglia, accumulo pendenti e andamento coda dove presente. HTTP 200 da solo non qualifica sostenibilità. Non fermarsi al primo rifiuto. Esito del tentativo, validità del carico e sostenibilità sono campi distinti; un gradino non qualificabile non attiva retry né selezione del tentativo migliore.
- [ ] Aggiungere i due confronti mirati e grafici temporali di carico, working set, CPU, latenza, coda ed errori con confini. Mostrare i tre run e dispersione osservata; niente gate statistici one-shot.
- [ ] Falliti, non applicabili e campioni insufficienti restano espliciti e distinti da zero. Evidenziare possibile collo di bottiglia della funzione e differenze di ammissione; non attribuire automaticamente il sovraccarico alla CPU del control plane.
- [ ] Testare fixture con risposta tardiva corretta, pendente finale, rifiuto, drop generatore, baseline senza coda, OOM e serie mancanti; ri-eseguire test e fixture dei report storici.
- [ ] Testare il caso richiesto 1.000/s, inviato 200/s, successo 100%, tolleranza rate 5%: esito non qualificabile a 1.000/s, con throughput osservato conservato. Coprire valore esattamente alla soglia (ammesso), superamento (non qualificabile), drop e ritardo indipendenti, recupero tardivo che nasconde un deficit nella media del gradino e prove generatore mancanti. Testare il report di avvio fallito senza summary e la selezione del tentativo dalla ricevuta.

## Task 8 — verifiche locali e consegna operativa

**File:** README NanoLab, README recipe, documentazione esperimento collegata al README; test già elencati e fixture minime. Nessun report o sistema di esecuzione separato.

- [ ] Dopo ciascun task eseguire i test interessati prima e dopo la modifica; separare diff/commit logici e non includere file estranei. Non creare commit durante la sola pianificazione.
- [ ] Verificare localmente SDK/provider con fake, schema/CLI, 21 celle e ordini persistenti, moduli/provider, flag/classificazione, una funzione/digest, prepare senza doppia build, identità/resume di successi e fallimenti, tentativi conservati, plateaux/coorti/pendenti/drop, sovrapposizione con `gracefulStop`, gate del generatore e packaging installato. Distinguere test su wheel Sonata locali da installazione pulita della coppia pubblicata richiesta dal Task 1.
- [ ] Eseguire smoke k6 breve verso fixture HTTP locale e test di report usando snapshot Prometheus simulati. Riportare separatamente ciò che questi test non provano: native reali, deploy k3s delle due composizioni, configurazione osservata, correttezza QR reale, C2, isolamento e saturazione.
- [ ] Eseguire suite NanoLab completa, lint e type check con i comandi effettivi del repository; eventuali baseline failure vanno distinte dalle regressioni. Nessun test deve chiamare Azure o avviare native lunghe.
- [ ] Documentare uso delle VM mantenute da compare, costi di supporto/k3s e dimensionamento necessario alle build distinto dal footprint misurato. Individuare e verificare il metodo di cleanup esistente del provider/lifecycle prima di pubblicarne il comando: non inventare `compare --teardown`.
- [ ] Consegnare diff, istruzioni concise, risultati locali con comandi/esiti, limiti e comandi Azure esatti verificati contro CLI/help e test. Nessun nome/flag proposto diventa disponibile solo perché appare in questo piano.

Comandi locali di partenza, da eseguire nell’implementazione con l’ambiente del repository disponibile:

```bash
uv lock --check
uv run --package nanolab pytest packages/nanolab/tests/comparison packages/nanolab/tests/cli/test_comparison.py packages/nanolab/tests/plans/test_runtime_comparison.py --no-cov
uv run --package nanolab pytest packages/nanolab/tests/config packages/nanolab/tests/images/test_control_plane_variants.py packages/nanolab/tests/tasks/loadtest packages/nanolab/tests/test_package_layout.py --no-cov
uv run --package nanolab pytest packages/nanolab/tests
uv run --package nanolab ruff check packages/nanolab
uv run --package nanolab basedpyright --project packages/nanolab/pyproject.toml
uv build --package nanolab --wheel --out-dir /tmp/nanolab-experiment-01-dist
```

Il test Sonata va eseguito nel suo ambiente corretto contro SDK 0.7.0, includendo `packages/sonata-tasks/tests/vm/test_azure_provider.py`. Il comando esatto di installazione del wheel e smoke k6 sarà fissato dopo avere implementato il test e verificato il tooling disponibile; non è una verifica già superata.

## Pilot e campagna: procedura successiva, non autorizzata da questo piano

1. Preparare il preset `azure-comparison` con credenziali/path operatore, VM stack e loadgen e OS pinned; mantenere le regole di rete esistenti.
2. Eseguire prepare e pilot tramite lo stesso `compare`, con calendario breve configurabile, payload iniziale e FIXED 2. Le build sono completate prima dell’inizio del carico.
3. Verificare sulle VM composizioni, flag effettivi, heap/limiti, una replica, digest comune, QR completo, coda realmente esercitata, ammissione baseline e strumenti/risoluzione. Con gli strumenti già disponibili osservare compilazioni di livello C2 durante warmup del full JIT; conservare la prova, senza dichiarare stabilizzazione senza evidenza. Eventuale strumentazione diagnostica solo pilot va dichiarata e non trasferita nascostamente alle misure.
4. Verificare sufficienza del carico QR e del generatore e attraversamento del sovraccarico per ogni configurazione eseguibile. Modificare manualmente payload/concorrenza/calendario se serve; ogni nuova identità richiede nuova directory. Nessun calibratore.
5. Congelare payload esatto, calendario e warmup comuni, durate, concorrenza, ammissione/coda per composizione, timeout/`gracefulStop`/margine/drain, budget VU sovrapposti, soglie latenza/successo, criterio pendenti/coda e soglie/finestre di fedeltà del generatore; riportare hash e ordine effettivo delle celle nel manifest. Non lanciare la campagna con calendario non confermato dal pilot. La fedeltà del generatore va comunque verificata in ogni gradino della campagna.
6. Eseguire la campagna completa di 21 run sugli stessi host negli ordini dichiarati, nuovo deployment per ogni cella/tentativo. Conservare fallimenti e sovraccarico sopra soglia entro limiti finiti; non aumentare heap/risorse. Il resume salta gli esiti definitivi validi, compresi OOM e avvii falliti, conservando l’ordine residuo.
7. Generare il report compare e conservare dati grezzi; eseguire cleanup esistente quando autorizzato, mantenendo gli artifact locali.

Forme previste dei comandi, **da rendere operative nell’implementazione**. Usano flag compare già esistenti, scenari nuovi e le sette chiavi della tabella:

```bash
export NANOFAAS_ROOT=/percorso/al/checkout/nanofaas
export E01_VARIANTS=experiment-01-core-k8s-jvm-jit,experiment-01-core-k8s-native-o3,experiment-01-core-k8s-queue-jvm-int,experiment-01-core-k8s-queue-jvm-c1,experiment-01-core-k8s-queue-jvm-jit,experiment-01-core-k8s-queue-native-o2,experiment-01-core-k8s-queue-native-o3

./nanolab.sh compare experiment-01-pilot.yaml --environment /percorso/azure-comparison.yaml --variants "$E01_VARIANTS" --repetitions 1 --run-dir /percorso/runs/experiment-01-pilot

./nanolab.sh compare experiment-01.yaml --environment /percorso/azure-comparison.yaml --variants "$E01_VARIANTS" --repetitions 3 --run-dir /percorso/runs/experiment-01-campaign
```

La seconda directory serve a separare protocollo congelato e pilot; la preparazione deve comunque rispettare l’identità degli artifact e dei due host. Non promettere riuso cross-directory non supportato: se il prepare esistente ripubblica, lo fa prima delle misure. Ripetere lo stesso comando riprende il relativo run solo sugli host originali e con identità invariata. Il calendario definitivo non può essere fissato onestamente prima del pilot.

## Criterio di completamento

La consegna dell’implementazione è completa quando test locali e smoke pertinenti passano, i confronti precedenti restano compatibili, il pacchetto installato contiene tutti gli asset, la compatibilità SDK è dimostrata e le istruzioni operative sono eseguibili. Devono essere verificati resume degli esiti definitivi senza perdita dei tentativi, ordine persistente, completamenti oltre i confini k6 e impossibilità di qualificare un rate non erogato dal generatore. L’ambiente consegnato deve risolversi e installarsi dai pin con SDK 0.7.0 e coppia Sonata pubblicata e coerente: i soli wheel locali non completano questo requisito; l’eventuale release mancante resta un prerequisito esterno aperto. Le verifiche Azure non ancora effettuate devono restare elencate: nessun risultato sperimentale, supporto native reale o soglia di capacità viene dedotto dai soli mock.
