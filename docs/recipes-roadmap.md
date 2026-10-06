# Roadmap NanoLab: recipes v2

Aggiornata il 2 ottobre 2026. Questa pagina raccoglie gli obiettivi di
migrazione e verifica; non sostituisce le specifiche e i piani di implementazione
in `docs/superpowers/`.

## Obiettivo

Semplificare NanoLab usando profili recipe riutilizzabili per descrivere le
build NanoFaaS. Rendere simmetrici i workflow quando condividono lo stesso
ciclo di build, pubblicazione e raccolta delle evidenze, mantenendo esplicite
le differenze reali fra backend e tipi di verifica.

## Stato attuale

- [x] Profilo JVM per la validazione container salvato in
  `packages/nanolab/recipes/validate-container-jvm.yaml`.
- [x] Task NanoLab parametrizzati dalla recipe per `assembleRecipe` e
  `publishRecipe`; il secondo assembla e pubblica nello stesso comando.
- [x] Workflow `deployment-lifecycle-container` migrato al profilo e al report
  `distribution.json`.
- [x] CI esegue `validateRecipe` sul profilo nei push/PR con checkout NanoFaaS.
- [x] Il ciclo container completo è stato eseguito localmente con esito positivo.

- [x] Profilo native e scenario gemello aggiunti; entrambi i profili sono
  validati dalla CI. I due cicli container sono verificati localmente.
- [x] Profilo Kubernetes JVM collegato allo scenario unico; il ciclo locale
  Minikube con immagini caricate, controlli Pod, coda e cleanup è verificato.
- [x] Il ciclo Multipass con staging e `publishRecipe` nella VM è verificato:
  identità delle immagini su k3s, invocazioni, coda, k6 e cleanup sono passati.
- [x] Containerd migrato con
  `packages/nanolab/recipes/validate-containerd-jvm.yaml` e
  `packages/nanolab/scenarios-v2/deployment-lifecycle-containerd.yaml`.
  `publishRecipe` nella VM fornisce JAR, immagini e `distribution.json`;
  metadati, digest dell'immagine in containerd, limiti OCI, invocazione e
  cleanup sono passati nel ciclo Multipass del 29 settembre 2026. Nessuna
  build legacy viene pianificata per questo scenario.

## Prossimi passi, in ordine

1. **Migrare load test e confronto dei runtime.** Usare profili recipe per le
   build, conservando immagini fissate per i test di carico e build eseguite
   sulla VM misurata dove richiesto. Fatto quando gli scenari producono le
   stesse misure e hanno meno logica di build duplicata.
   - [x] Primo sottoinsieme container: `autoscaling-cycle-container.yaml`
     usa `loadtest-container-jvm.yaml`. Verificato il 29 settembre 2026:
     una pubblicazione, k6, identità delle immagini, 69 query Prometheus,
     autoscaling da 0 a 5 e ritorno a 0, report e cleanup.
   - [x] Preparazione recipe di `nanolab compare`: nove profili validati;
     cella JVM su Multipass verificata il 30 settembre 2026, commit NanoLab
     `9333d77`, NanoFaaS `e7914be0`. Una pubblicazione, C1 e tre immagini
     verificate, scheduler unified/per-function, k6, snapshot e report.
     Evidenze: `/tmp/nanolab-comparison-recipe-e2e-20260930e/`.
     Ripresa senza build/carico e rifiuto di input diversi con `--fresh`
     verificati; VM originale conservata. Vedi il
     [contratto operativo](../packages/nanolab/README.md#recipe-runtime-comparison).
   - [ ] Pubblicazione delle varianti native, Oracle G1 e matrice completa.
   - [ ] Migrazione dei load test degli altri backend.
2. **Coprire le altre capacità v2 con profili mirati.** Aggiungere casi per
   Bash, servizi e poi multiarch. Fatto quando ciascun caso ha un profilo e
   una verifica delle evidenze adatte; per multiarch usare digest e manifest,
   non un image ID locale.
   - [x] Bash: `validate-container-bash.yaml` e scenario
     `deployment-lifecycle-container-bash.yaml`, verificati il 30 settembre 2026
     con Docker sull'host e NanoFaaS `e7914be0`. Recipe validata, una
     pubblicazione, metadati, immagini, invocazione, risorse e cleanup passati.
     Mapping condiviso `exec` → SDK recipe `bash`; nessun nuovo task.
     Evidenze: `/tmp/nanolab-recipe-bash-e2e-20260930/`, log `.log` a lato.
   - [x] Servizi: copertura mirata di Java JVM/native e artefatto Dockerfile.
     - [x] Java JVM: `validate-container-services-jvm.yaml` e scenario
       `deployment-lifecycle-container-services.yaml`, verificati con Docker
       locale il 30 settembre 2026 su NanoFaaS `e7914be0`. Una pubblicazione,
       metadati, tre immagini, invocazioni, output echo esatto, ispezione dei
       container e cleanup passati. Nessun nuovo task o campo di scenario.
       Evidenze: `/tmp/nanolab-recipe-services-e2e-20260930/`, log `.log` a lato.
     - [x] Servizi native e selezione della build indipendente dal control plane:
       profili `validate-container-jvm-service-native.yaml` e
       `validate-container-native-service-jvm.yaml`, con scenari omonimi
       `deployment-lifecycle-container-*`. Entrambi i cicli Docker locali
       sono passati il 30 settembre 2026 su NanoFaaS `e7914be0`, mantenendo
       word-stats JVM: una pubblicazione per ciclo, modalità indipendenti,
       metadati, tre immagini, invocazioni, output echo esatto, ispezione e
       cleanup. Builder container Community, Serial GC e ottimizzazione 3.
       Evidenze: `/tmp/nanolab-jvm-service-native-e2e-20260930/` e
       `/tmp/nanolab-native-service-jvm-e2e-20260930/`, log `.log` a lato.
       Nessuna modifica ai task; la matrice runtime native resta da verificare.
     - [x] Servizi Dockerfile: `validate-container-watchdog.yaml` e scenario
       `deployment-lifecycle-container-watchdog.yaml`, verificati con Docker
       locale il 30 settembre 2026 su NanoFaaS `e7914be0`. Una pubblicazione,
       contesto `runtimes/watchdog`, tre image ID, versione watchdog `0.22.0`
       coerente con Cargo.toml, exit code zero, invocazione della funzione e
       cleanup passati. Riutilizzati i task Docker Sonata; collisioni sul nome
       del container e cleanup dei soli ID creati verificati nei test.
       Evidenze: `/tmp/nanolab-recipe-watchdog-e2e-20260930/`, log `.log` a lato.
       Questa tappa verifica l'artefatto; i comportamenti del supervisore
       restano coperti dai test specifici del runtime watchdog.
   - [x] Multiarch JVM: `validate-container-multiarch-jvm.yaml` e scenario
     `deployment-lifecycle-container-multiarch.yaml`, verificati il 30 settembre
     2026 con Docker sull'host ARM64 e NanoFaaS `e7914be0`. Una pubblicazione,
     indice e manifest/configurazioni AMD64/ARM64 verificati sui byte originali,
     immagini host fissate per digest, metadati, invocazione word-stats e
     cleanup passati (15 task). Builder dedicato e QEMU AMD64 temporaneo;
     preservati builder selezionato e risorse preesistenti. La prova runtime
     riguarda ARM64; build native e invocazioni su entrambe le architetture
     restano fuori da questo sottoinsieme.
     Evidenze: `/tmp/nanolab-multiarch-recipe-e2e-20260930/run-3/`, log
     `e2e-3.log` a lato; regressione JVM ordinaria passata (13 task). Il primo tentativo ha verificato la compensazione
     dopo il fallimento del probe; corretto il pin dei probe per piattaforma.
3. **Affrontare soak e release.** Migrare soltanto le parti equivalenti del
   processo di build. Fatto quando restano validi gli snapshot immutabili, la
   provenienza, la firma e i contratti operativi esistenti.
   - [x] Preparazione recipe del container smoke ARM64: profilo
     `soak-container-smoke-jvm.yaml` e scenario
     `memory-soak-smoke-recipe-container.yaml`. Un solo snapshot/pubblicazione,
     provenienza massima, compiler effettivi, digest/configurazioni e report
     offline verificati. Il ciclo completo ha eseguito tutte le fasi e
     122 richieste steady, zero errori/drop; cleanup e regressione JVM ordinaria
     passati. Evidenze: `/tmp/nanolab-soak-recipes-e2e-20260930/run-8/`.
   - [ ] Accettazione del container smoke: report `INCONCLUSIVE` per crescita
     RSS dei tre processi e attribuzione ownership/equal-work mancante. I gate
     di provenienza, preflight, carico e integrità passano; soglie immutate,
     `p24_qualified: false`. Revisione dei limiti rinviata separatamente su
     indicazione dell’utente. Vedi [risultati e confini](soak.md#local-verification).
   - [ ] Release AMD64: migrazione della build a tre profili recipe mantenendo
     la fase separata di push e i contratti di benchmark/firma.
     Implementazione locale completata: tre profili riutilizzabili (9/12/23
     immagini), inventario dell'archivio, report e ID locali verificati,
     fingerprint e resume coperti dai test. Benchmark,
     pubblicazione e firma conservano i gate esistenti.
     `validateRecipe` e il preflight reale con NanoFaaS `e7914be0` passano.
     Verifica nativa AMD64 ancora incompleta: Azure ha rifiutato la creazione
     della VM richiedendo MFA. Restano build/export reali, push di staging,
     runtime rappresentativi, resume e teardown. Nessuna release qualificata.
     [Spec approvata](superpowers/specs/2026-10-01-release-amd64-recipes-design.md),
     [piano e stato delle verifiche](superpowers/plans/2026-10-01-release-amd64-recipes.md).
   - [ ] Release ARM64: implementazione locale completata con tre profili
     simmetrici AMD64/ARM64, assembly e push distinti, prove locali sulla VM
     proprietaria e resume verificato nei test. I vecchi journal del DAG
     combinato richiedono un nuovo run-dir; restano conservati.
     Sei `validateRecipe` e preflight eseguibile passano con NanoFaaS `e7914be0`.
     Il 2 ottobre 2026 la prova canonica si è fermata prima delle build: Azure
     rifiuta la creazione delle risorse di rete senza MFA. Capability nativa ARM,
     slice reale di 44 immagini/push/runtime e resume reale ancora incompleti.
     Nessuna release qualificata; gate AMD64 indipendente ancora pendente.
     [Spec](superpowers/specs/2026-10-02-release-arm64-recipes-design.md),
     [piano e verifiche](superpowers/plans/2026-10-02-release-arm64-recipes.md).
   - [ ] Migrazione dei preset P24 e degli altri backend soak.
     Il preset canonico `memory-soak-sync-container.yaml` ora usa
     `soak-container-p24-jvm.yaml`, conservando policy, fasi e carico (20/20).
     Validazione Gradle e pubblicazione nativa ARM64 delle tre immagini,
     provenienza, ricevute offline e tuning JVM verificati con NanoFaaS `e7914be0`.
     Preparazione completa ancora incompleta: preload diagnostico Node assente
     e cinque coperture dei prerequisiti senza ricetta di iniezione automatica.
     Nessuna campagna P24 eseguita; `p24_qualified: false`. Altri preset/backend
     restano da migrare. [Gate e prove](soak.md#p24-preparation-verification),
     [piano](superpowers/plans/2026-10-02-soak-p24-recipes.md).
4. **Eliminare i percorsi legacy.** Rimuovere build duplicate e flag di
   scenario ridondanti dopo la migrazione dei rispettivi workflow. Fatto
   quando non restano chiamanti e la suite pertinente passa.
   - [x] La richiesta condivisa `PlatformRequest` disattiva build e push legacy
     quando riceve una `RecipeBinding`, anche tramite `dataclasses.replace`.
     Rimossi i flag duplicati dai planner `validate` e `loadtest`. Verificati
     i tre backend nella compilazione dei piani e il ciclo container JVM reale:
     13 task passati, una sola `publishRecipe`, metadati, immagini, invocazione,
     limiti e cleanup. I percorsi senza recipe e con immagini precompilate
     conservano le proprie opzioni.
   - [ ] Migrare i chiamanti rimanenti prima di rimuovere le build legacy:
     load test degli altri backend, offload e soak containerd. La fase runtime
     del soak usa già immagini fissate senza build e conserva i suoi guardrail.

Per ogni tappa, aggiornare questa pagina con lo stato e collegare il profilo,
lo scenario o la verifica introdotti.
