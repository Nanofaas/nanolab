# GC/JIT/native Campaign Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rendere NanoLab pronto a ripetere lo studio GC/JIT/native sulla build attuale di NanoFaaS.

**Architecture:** estendere compare/experiment-01 con sette profili a moduli identici; riutilizzare ledger, recipe publication e receipt. Usare il container builder nativo esistente in NanoFaaS e adeguare la verifica del suo report, senza un nuovo workflow.

**Tech Stack:** Python 3.12, Pydantic, pytest, YAML, k6, Sonata, Gradle recipe v2, Docker/BuildKit, k3s, Prometheus/cAdvisor.

**Spec:** `docs/superpowers/specs/2026-10-30-gc-native-refresh.md`.

## Global Constraints

- Implementazione autorizzata e completata sul branch feat/gc-native-refresh; vedere il ledger e le evidenze finali. Le attivita di pilot cloud restano future.
- NanoLab aggiornato con git pull --ff-only: fcbbbca -> 6f32a3f3770a59b50ef96338de5a52f2b69c2b59; checkout iniziale e post-pull puliti.
- NanoFaaS osservato, senza aggiornarlo: c0d15e3d1c132ee9691f4d3dd34b9b611b92cedd, checkout pulito; coincide col pin in .github/actions/setup-workspace/action.yml.
- Pin osservati: Java 25, GraalVM Community release 25.2.4 / Java 25.0.4, plugin io.github.nanofaas.build 0.1.2. Non sostituire il pin con un main mobile.
- Per la campagna registrare anche SHA NanoLab finale, plugin risolto, builder image/base image digest, architettura e identità host; non usare solo tag mutabili come identità.
- Sette chiavi runtime esistenti e tre moduli identici come da specifica; heap iniziale comune 64 MiB; builder: container per le tre nuove recipe native.
- Lasciare invariati default e semantica dei confronti storici, experiment-01 esistente ed experiment-02; preservare archivi e rifiuto di resume incompatibili.
- Stesso digest funzione e calendario entro ogni confronto; build terminate prima di idle e carico.
- Nessuna dipendenza aggiuntiva, nuova orchestrazione, GC forzata o cancellazione di evidenze.
- Oracle/G1 resta selezione esplicita, con distribuzione e monitoring registrati; nessuna equivalenza causale automatica con Community/Serial.

## Review Focus

1. Container builder aggiunge distribution ai metadati: accettarlo solo quando atteso e verificato, senza allentare tutti i confronti dict (Task 1).
2. Riutilizzare chiavi runtime in un altro profilo non deve selezionare recipe o publisher del confronto storico (Task 2).
3. G1 può non avviarsi con heap iniziale; fallimento conservato, nessun aumento automatico (Task 3).
4. Native G1 senza MXBean: metrica mancante distinta da zero e JFR non equiparato alle pause JVM (Task 4).
5. Saturazione della funzione/generatore o CPU a cavallo delle fasi non deve diventare una conclusione sul control plane (Task 5).

## Evidenze e punti da correggere

- Le nove VARIANTS storiche contengono già i confronti GC/tiering/native e due diagnostiche event-loop, ma il vecchio calendario usa due word-stats.
- Le sette EXPERIMENT_01_VARIANTS attuali sono tutte Serial/64 MiB e cambiano anche i moduli; non sono la nuova matrice GC.
- comparison/profiles.py::_require_build vieta G1 e monitoring; RuntimeClass non include native-os.
- comparison/prepare.py invoca native_host_command solo per experiment-02; native_host.py installa solo Community in /opt/graalvm.
- NanoFaaS docs/recipes.md documenta builder: container, selezione Oracle per G1 e distribution nel report. comparison/evidence.py confronta cp.native e options[native] per uguaglianza esatta; declared_options non include distribution.
- metrics/catalogue.py possiede già query GC/MXBean/JFR e giustificazioni delle assenze; non ricreare i meter.
- experiment-01-summary non produce ancora i grafici temporali richiesti; il preset frozen contiene valori pilota/placeholder.
- Baseline verificata dopo pull: 64 test passati in 1.91 s su profiles, matrix, configurazione experiment-01 e native_host. Nessuna build o misura eseguita.

## Esito della revisione del 30 ottobre: riuso prima delle aggiunte

| Responsabilità | Implementazione da riusare | Aggiustamento necessario |
| --- | --- | --- |
| Schema e vincoli recipe | Plugin 0.1.2, validateRecipe | Solo verifiche dei vincoli sperimentali in NanoLab |
| Build e pubblicazione | recipe_command / publishRecipe | Usare container nelle recipe; nessuna assemble separata |
| GraalVM, librerie G1, packaging | tools/native-java/Dockerfile, install-graalvm.sh | Nessun installer o Dockerfile nuovo |
| Identità | build.variant, /modules/build-metadata, distribution.json, receipt | Verificare distribution per container e distinguere i due gruppi |
| Varianti | VARIANTS_BY_KEY | Selezionare sette chiavi, evitare duplicato EXPERIMENT_01_RUNTIME_VARIANTS |
| Calendarizzazione e ledger | Experiment01Protocol, experiment-01.js | Collegare il contratto dei runtime alle funzioni esistenti |
| Contatori | metrics/interpretation.py::counter_delta | Selezione per fase; conservare labels e reset |
| Grafici | loadtest/report.py e comparison_report.py | Adattamento degli input, non un nuovo renderer |

La verifica del codice NanoFaaS conferma che tools/native-java/Dockerfile installa
anche gcc-c++ per G1 e conserva le librerie native accanto all'eseguibile. Non
ricostruire manualmente queste immagini in NanoLab. Il plugin esterno possiede
schema e recipe orchestration; Dockerfile e installer appartengono a NanoFaaS
(docs/gradle-plugin.md). Non è necessario cambiarli per questo studio.

Lo schema recipe-v2.schema.json è stato letto direttamente dal JAR 0.1.2 nella
cache Gradle: builder container, variant, G1 e maxHeap sono già supportati.
La verifica sintattica non dimostra una build funzionante. Il container builder
semplice non garantisce da solo attestazioni della compilazione: acquisire log,
report e digest disponibili; non introdurre multiarch/provenance/QEMU come
requisito di questa campagna. Se un'identità del builder manca, registrare il
limite, senza inventare un digest. Una macchina nativa per architettura.

GitNexus sul checkout /Users/micheleciavotta/Downloads/nanofaas è indicizzato al
commit e35405e del 3 ottobre, antecedente al pin corrente; la query non ha
restituito flussi recipe utilizzabili. I riscontri sono quindi sui documenti,
Dockerfile e schema del plugin attuali; nessuna conclusione di impatto è tratta
dal grafo obsoleto e nessun codice NanoFaaS è modificato.

### Task 1: Compatibilità del container builder con identità e receipt

**Files:** `packages/nanolab/src/nanolab/comparison/{profiles,evidence,prepare}.py`; `packages/nanolab/src/nanolab/tasks/recipes/workflow.py` solo se il parser richiede cambi; test in `tests/comparison/{test_profiles,test_evidence,test_prepare}.py`, `tests/tasks/recipes/test_workflow.py` (tutti sotto packages/nanolab).

**Interfaces:** mantenere declared_options(profile, *, modules=...) -> dict, read_distribution e verify_comparison_publication(...) -> receipt; l'identità attesa deve includere distribution solo per builder container.

- [ ] Aggiungere test: Serial/container richiede Community, G1/container richiede Oracle e jfr; distribuzione, GC, builder, heap o optimization errati sono respinti. Fixture host storiche invariate.
- [ ] Eseguire i test nuovi e verificare il fallimento per la differenza di metadati.
- [ ] Eseguire validateRecipe sul pin per la validazione ufficiale; poi publishRecipe una sola volta per profilo, che include già assemble. Nessun nuovo validatore YAML/schema, installer o traduttore di flag. Tenere log fuori da PrecipeOutput (directory rigenerata) e output fuori dal contesto Docker o sotto build/.
- [ ] Adeguare dichiarazione e verifica allo schema effettivo di distribution.json del pin. Conservare i controlli esatti su source, recipe, digest, moduli e opzioni. Se manca la distribuzione attesa nel nuovo profilo, fallire.
- [ ] Verificare che le recipe container non invochino native_host_command e che experiment-02 mantenga il proprio percorso host. Non modificare l'installer Community per supportare questa campagna.
- [ ] Test mirati: `.venv/bin/python -m pytest -c packages/nanolab/pyproject.toml -o addopts='' packages/nanolab/tests/comparison/test_profiles.py packages/nanolab/tests/comparison/test_evidence.py packages/nanolab/tests/comparison/test_prepare.py packages/nanolab/tests/tasks/recipes/test_workflow.py -q`.
- [ ] Commit separato dopo verifica, nell'implementazione futura.

### Task 2: Collegare i runtime esistenti al protocollo experiment-01

**Files:** sotto packages/nanolab, `src/nanolab/comparison/{profiles,matrix,manifest,experiment_01}.py`, `src/nanolab/cli/comparison.py`, `src/nanolab/plans/runtime_comparison.py`; `src/nanolab/images/control_plane_variants.py` solo per classificare correttamente native-os. Creare sette recipe `src/nanolab/assets/presets/recipes/experiment-01-runtime-<key>.yaml` per le chiavi della specifica. Test: `tests/comparison/{test_experiment_01,test_matrix,test_manifest,test_profiles}.py`, `tests/cli/test_comparison.py`, `tests/plans/test_runtime_comparison.py`.

**Interfaces:** riusare VARIANTS_BY_KEY/resolve_variants, ProfileContract, declared_options e PreparedComparison. Una sola tupla di selezione `("jvm", "jvm-g1", "jvm-c2", "jvm-g1-c2", "native-os", "native-o3", "native-o3-g1")`; niente nuovo catalogo di ControlPlaneVariant e niente nuovi build_env. La recipe è la fonte delle opzioni da costruire; il contratto della campagna verifica solo i vincoli scientifici.

- [ ] Testare che experiment-01 con queste chiavi risolva le recipe di campagna, mentre loadProfile comparison continui a risolvere comparison-<key>.yaml. Le chiavi experiment-01-core-* continuano a selezionare le recipe originarie; selezioni miste sono rifiutate.
- [ ] Derivare le sette recipe dalle comparison-* esistenti, mantenendo jvm.args e native optimization/gc. Impostare moduli comuni, heap iniziale esplicito, builder container per native, nomi immagine distinti e build.variant uguale alla chiave. Non copiare la logica di generazione dei flag del plugin.
- [ ] Solo experiment-01-runtime-jvm.yaml pubblica qr-code/rust. Il gruppo runtime usa publisher jvm e QR; il gruppo storico comparison usa publisher jvm e word-stats: risolvere entrambi tramite ProfileContract e identità, mai dal solo nome jvm.
- [ ] Adattare il contratto experiment-01 per distinguere il gruppo originario dal gruppo runtime. Riutilizzare declared_options per l'evidenza delle opzioni; non allentare _require_build condiviso con experiment-02 per accettare arbitrariamente G1. Richiedere gli stessi moduli, heap e parametri comuni nelle sette recipe, con le sole differenze dichiarate GC/JIT/ottimizzazione.
- [ ] Usare contract.modules(key) e runtime_class(variant) dove il percorso experiment-01 oggi assume variant.modules/runtime, perché gli oggetti storici non dichiarano quei campi come le varianti E01. Aggiungere native-os a RuntimeClass e alla classificazione già esistente, senza un secondo classificatore.
- [ ] Riutilizzare i tre ordini EXPERIMENT_01_ORDERS parametrizzando il catalogo di riferimento di build_experiment_01_matrix con default originario. Per il nuovo gruppo passare le sette varianti esistenti nell'ordine della tupla; subset filtrati. Salvare gruppo e ordine effettivi nell'identità e distinguere la nuova orderPolicy.
- [ ] Riutilizzare il discriminante già disponibile nelle chiavi builds dell'identità per scegliere ProfileContract: experiment-01 con chiavi runtime è il nuovo gruppo, experiment-01-core-* quello originario. Rifiutare chiavi miste/sconosciute. Niente nuovo motore di registrazione.
- [ ] Testare default storici, subset senza publisher, moduli identici, recipe alterata, gruppo errato e resume incompatibile; confrontare serializzazione delle fixture storiche. Eseguire i test elencati e i contratti reali sul pin.
- [ ] Commit separato dopo verifica, nell'implementazione futura.

### Task 3: Pilot riproducibile e controlli prima del carico

**Files:** creare `src/nanolab/assets/presets/scenarios/experiment-01-runtime-gc-pilot-cpu{1,2,4}.yaml`; riusare payload esistente. Modificare `src/nanolab/plans/runtime_comparison.py` ed `evidence.py` soltanto per evidenze mancanti. Test `tests/plans/test_runtime_comparison.py`, `tests/comparison/test_experiment_01.py`, `tests/cli/test_comparison.py`.

**Interfaces:** nuovi preset restano workflow loadtest/backend k8s/loadProfile experiment-01; selezione esplicita delle sette chiavi runtime esistenti in compare. Nessun nuovo flag CPU o heap necessario per il pilot.

- [ ] Testare che solo requests/limits CPU del control plane cambino fra i tre preset; moduli, heap, immagine funzione, payload, coda e concorrenza sono uguali. Impostare inizialmente 512 MiB container e 64 MiB heap; valori esplicitamente provvisori.
- [ ] Salvare pod spec, launch args JVM, build-metadata, opzioni native effettive, limiti cgroup, versioni Java/native-image disponibili nei log/report del builder e digest effettivi prima del carico; non presumere attestazioni assenti nel percorso scelto. Non chiamare prova di C2 un semplice flag full tiering.
- [ ] Provare classificazione startup_failed/OOM e conservazione delle evidenze anche per G1. Non aumentare heap o memoria quando un profilo fallisce.
- [ ] Per l'avvio registrare confini distinti: provisioning/build, launch/readiness, funzione pronta, prima invocazione. Non ricavare costo di compilazione dal tempo di esecuzione della cella.
- [ ] Verificare piani e test senza provisioning. Lasciare status: pilot e non creare un nuovo preset frozen con valori non calibrati; commit separato dopo verifica.

### Task 4: Evidenze GC e report rigenerabile

**Files:** `src/nanolab/metrics/catalogue.py` solo per gap dimostrati; `src/nanolab/comparison/experiment_01.py`; `src/nanolab/tasks/loadtest/{report,comparison_report}.py` per riusare rendering e grafici; `src/nanolab/metrics/interpretation.py` solo per gap dimostrati; `src/nanolab/cli/comparison.py` per il report esistente. Test `tests/comparison/test_experiment_01.py`, `tests/cli/test_comparison_report.py` e test metriche esistenti.

**Interfaces:** estendere summarize_phases(...) e write_experiment_01_report(...) conservando JSON/CSV attuali; aggiungere colonne descrittive/GC e grafici senza un secondo parser del ledger. Riutilizzare `ReportPhase`, i grafici di `tasks/loadtest/report.py`, `_over_time` e `_spread_chart` del comparison report dove gli input coincidono. Adattare i dati ai renderer esistenti; non creare experiment_01_figures.py o un nuovo sistema di report. Le aggregazioni storiche che ignorano le coorti non vanno riutilizzate per le latenze E01.

- [ ] Usare `metrics/interpretation.py::counter_delta` per i contatori, preservando labels e reset; limitarsi a selezionare i campioni della fase. Fixture con counter GC noti: delta nella fase, reset del counter segnalato, dati mancanti null, zero reale distinto; non sommare MXBean e JFR della stessa raccolta.
- [ ] Conservare dati grezzi e fonte delle metriche; rendere esplicita l'indisponibilità di heap/GC MXBean per G1 nativo. JFR total presente e filtro GC vuoto non significa zero GC.
- [ ] Report temporale allineato con carico, working set, CPU/throttling, latenza, coda e GC; grafici comparativi per CPU/variante. Con 5 s di raccolta e CPU su 30 s non inventare risoluzione maggiore.
- [ ] Calcolare latenze dalla coorte di invio e conservare errori, rifiuti, timeout, pending e dropped separati; non cambiare le definizioni del protocollo esistente.
- [ ] Per i riepiloghi GC usare differenze di contatori che coprono la fase; segnalare bordi non osservati. Non stimare pause individuali da contatori aggregati.
- [ ] Testare report offline da archivio estratto, senza checkout NanoFaaS/VM; stessi riepiloghi e dati grafici. Commit separato dopo verifica.

### Task 5: Verifica completa e documento operativo per il pilot

**Files:** aggiornare `docs/experiment-01.md`; creare `docs/runtime-gc-comparison.md`; aggiornare `scripts/smoke-installed-wheel.py` e test asset/recipe esistenti per i nuovi file.

**Interfaces:** soli comandi compare/compare-report esistenti; documentare le sette chiavi esplicite e i tre preset. Nessun lancio Azure in questa fase di pianificazione.

- [ ] Eseguire pure tests, contratti NanoFaaS sul pin esatto, type-check, import contracts e hook con gli stessi comandi di .github/workflows/ci.yml; usare Linux per i gate che lo richiedono. Non presentare pure-only coverage come copertura combinata.
- [ ] Installare la wheel fuori dal checkout e verificare sette recipe, tre preset, payload e script k6; validateRecipe per tutte le nuove recipe.
- [ ] Fare in una fase successiva uno smoke reale delle build JVM e delle tre native con builder container su Linux/architettura target, inclusi readiness e correttezza QR. I mock non dimostrano che G1 compili o parta.
- [ ] Pilot successivo: controllare risorse della funzione/generatore, emissioni e dropped, throttling, flags, GC/JFR e durata di warmup. Un plateau limitato dalla funzione non misura la capacità del solo control plane; ricalibrare prima del freeze, preservando il run.
- [ ] Produrre la proposta finale con SHA/digest, calendario, CPU, memoria/heap, criteri, ripetizioni scelte dal pilot, durata/costo e cleanup. Non riutilizzare valori placeholder del vecchio frozen.
- [ ] Prima di eseguire un pilot cloud concordare risorse e lifecycle in una proposta separata. Implementazione o successo dei test non autorizzano spese o campagna.

## Stato storico al termine della pianificazione

Pull completato e 64 test mirati passati. Nessun file di codice modificato,
nessun nuovo artefatto costruito, nessuna VM avviata, nessun commit/push.
Revisione successiva: controllati documenti/build NanoFaaS, schema dal JAR 0.1.2
e helper NanoLab; i 64 test citati appartengono alla verifica precedente.
I documenti sono una proposta di implementazione da rivedere, non un esito
sperimentale. I contratti container builder e la fattibilità di G1/64 MiB
restano da verificare realmente nei task indicati.

## Esito implementazione

Task 1–4 implementati e verificati; Task 5 completato per test, pacchetti,
validateRecipe e smoke locali reali delle sette build. Le caselle precedenti
sono il piano originario: il ledger e docs/runtime-gc-comparison.md descrivono
l'esito e le deviazioni. Build locali con assembleRecipe, senza publish remoto;
nessun nuovo percorso di build nel prodotto. Un unico commit locale raccoglie
l'implementazione verificata, anziche commit intermedi per task.

Restano intenzionalmente future la calibrazione su Kubernetes, la proposta
risorse/lifecycle e il freeze della campagna dopo il pilot. Nessuna infrastruttura
cloud e stata allocata e nessuna misura locale e presentata come risultato.
