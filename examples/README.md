# Disposable demo

This demo creates its own temporary directory, checkpoints one file, rewinds a
broken edit, redoes it, and removes the fixture. It never uses the directory
from which it is launched:

```console
python examples/disposable_demo.py
```

Expected output is similar to:

```text
checkpoint #1: version one
edited: broken edit
rewound: version one
redo: broken edit
temporary fixture removed: True
```
