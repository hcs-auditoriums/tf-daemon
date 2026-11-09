"""Vocal EQ assist: suggest EQ for a vocal mic from what it hears; apply it to the desk on request.

- voice:   the voice on a mic: speech activity, pitch, long-term spectrum
- tone:    EQ suggestions from that spectrum (against reference speech)
- plan:    the desk changes that make a channel's EQ a suggestion
- session: mic checks: listen while the person talks, then analyze
- desk:    apply / undo on the desk, only on a person's confirmed request
- ports:   what this needs from audio and the console (Protocols)
- module:  VocalModule: wiring and the /api/vocal routes
"""
