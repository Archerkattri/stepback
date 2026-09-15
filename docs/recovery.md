# Restore recovery contract

StepBack records a write-ahead journal at `.git/stepback/operation.json` in a
shared repository, or under the private `.stepback` store in shadow mode.

The journal is written before file replacement and advances through:

`prepared` -> `recovery-saved` -> `applying` -> `verified`

Normal completion removes the journal. If the process dies or a restore fails,
the record remains with `failed` (or an intermediate phase). A new process does
not mutate files automatically. Run:

```console
stepback recover
```

This restores the pre-operation file tree, verifies the result using the same
tree comparison as normal restore, removes the temporary recovery ref, and
leaves the redo entry available when applicable. Recovery is deliberately an
explicit action so a stale journal cannot surprise a user opening the project.

The contract covers file bytes, paths, deletions and file/directory transitions.
The journal does not claim transactional rollback for arbitrary external
readers, remote services, databases, deployments or third-party actions. It
also does not make conversation adapters atomic; those remain best-effort and
adapter-dependent. A partial filesystem restore is therefore reported as a
failure, and the user gets a recoverable file-state path rather than a silent
success.

The fault-injection regression in `tests/test_engine.py` kills a selective
restore after a partial write, constructs a fresh `Engine`, runs recovery, and
checks that both the selected and untouched files return to the pre-operation
state.
