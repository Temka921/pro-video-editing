#!/usr/bin/env python3
"""vedit: a small video editor for an AI to drive and a human to steer.

    python3 editor/vedit.py new myfilm
    python3 editor/vedit.py -p myfilm import clip.mp4
    python3 editor/vedit.py -p myfilm add a1
    python3 editor/vedit.py -p myfilm render --preview
    python3 editor/vedit.py -p myfilm serve        # visual editor in the browser

Run with --help for every command. See references/editor.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veditlib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
