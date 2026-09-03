# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Heresay — a telnet-talker-style chat engine (NUTS lineage).

Phase 1: a usable talker over a socket. Presence over persistence —
conversation is never written to disk.
"""

# The one source of truth for the version, read by `.version` and nothing else.
# Semantics the help page states out loud, so the number means something: the
# middle digit moves when behaviour changes, the last for a fix or for anything
# that executes no differently at all (prose, comments, tooling), the first for a
# change to the covenant itself. The full rule is CLAUDE.md, "The version number".
__version__ = "1.63.0"
