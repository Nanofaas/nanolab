# Revisione Sonata, Nanolab e Workbench — esito

I dieci rilievi iniziali sono corretti nei worktree isolati sul branch
`codex/review-remediation`. La successiva richiesta Terraform/OpenTofu è
implementata nello stesso lavoro, con responsabilità generiche in Sonata e
configurazione delle flotte in Nanolab.

| Rilievo iniziale | Correzione | Commit |
| --- | --- | --- |
| R1 / N1 | conservazione VM prima di qualunque export successivo se fallisce l'archivio collector | Nanolab `663a6d0d` |
| R2 / S1 | proprietà dei membri composite registrata prima del journal terminale | Sonata `71bc2d6` |
| R3 / N2 | cpuset propagato a generatore e osservatori operatore | Nanolab `21d4f9bf` |
| R4 / N3 | container obbligatori assenti rendono coverage incompleta | Nanolab `3bb4dc31` |
| R5 / S2 | fingerprint propagati dai task avvolti | Sonata `5193036` |
| R6 / W2 | listener, rete pod e Service allineati; admin API non attiva nei pod | Workbench `6533aa1` |
| R7 / W3 | loadgen vincolato al nodo con i suoi file, avvio reale verificato | Workbench `5105696` |
| R8 / W1+N4 | archivi qualificati per alias host, produttore e consumatori aggiornati | Workbench `201cc83`, Nanolab `75caf05b` |
| R9 / N5 | confronto evidenze per percorso e trial | Nanolab `3e3234df` |
| R10 / W4 | kind JSON non stringa produce HTTP 400 | Workbench `c635be6` |

Il [dossier Terraform e collaudo congiunto](../2026-11-10-terraform-stack/README.md)
riporta versioni, suite complete Linux, test Docker/kind, test delle wheel
installate, prove reali di guasto, artefatti e limiti. Le prove non eseguite sono
indicate esplicitamente; l'accettazione scientifica a dieci VM resta aperta.

[Registro iniziale di esecuzione](execution-ledger.md). I log dei test prima rossi
e poi verdi sono conservati nell'archivio permanente esterno indicato nel dossier.
Non sono stati pubblicati pacchetti, aperte PR o modificati i checkout originali.
