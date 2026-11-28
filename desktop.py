# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""HB Continuum Tk workstation entry point.

The implementation lives in :mod:`hb_engine.tk.app`. This wrapper keeps
``python3 desktop.py`` working for anyone who starts the legacy interface
directly.
"""
from hb_engine.tk.app import launch

if __name__ == '__main__':
    launch()
