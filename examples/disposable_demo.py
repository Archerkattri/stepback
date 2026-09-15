"""Run a complete StepBack journey in a disposable temporary directory."""

from __future__ import annotations

import tempfile
from pathlib import Path

from stepback import Engine


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stepback-demo-") as raw:
        work = Path(raw)
        document = work / "note.txt"
        document.write_text("version one\n")

        engine = Engine(work)
        checkpoint = engine.checkpoint(label="known good")
        assert checkpoint is not None
        print(f"checkpoint #{checkpoint.id}: {document.read_text().strip()}")

        document.write_text("broken edit\n")
        print(f"edited: {document.read_text().strip()}")
        engine.rewind(checkpoint)
        print(f"rewound: {document.read_text().strip()}")

        engine.redo()
        print(f"redo: {document.read_text().strip()}")
    print(f"temporary fixture removed: {not work.exists()}")


if __name__ == "__main__":
    main()
