# Roadmap NanoLab: recipes v2

Aggiornata il 30 settembre 2026. Questa pagina raccoglie gli obiettivi di
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
4. **Eliminare i percorsi legacy.** Rimuovere build duplicate e flag di
   scenario ridondanti dopo la migrazione dei rispettivi workflow. Fatto
   quando non restano chiamanti e la suite pertinente passa.

Per ogni tappa, aggiornare questa pagina con lo stato e collegare il profilo,
lo scenario o la verifica introdotti.
