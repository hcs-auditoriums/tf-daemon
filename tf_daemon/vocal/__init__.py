"""Vocal EQ assist: suggest EQ for a vocal mic from a mic check; apply it to the desk on request.

- module: VocalModule, the mic-check sessions and /api/vocal routes
- plan:   the desk changes that make a channel's EQ the suggestion
- the analysis itself is signal processing: dsp/vocal.py
"""
