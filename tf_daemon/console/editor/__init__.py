"""The TF Editor protocol (TCP 50368): how the console's EQ is read, and written on request.

The TF5's text protocol (RCP, see console/rcp.py) has no EQ addresses, so the EQ is
read the way TF Editor / StageMix read it. Protocol details and how they were
established: guides/tf5_api.md.

    -> MPRO subscribe "Property", hash compare (zero data hash), sync open
    <- MPRO hash compare, sync open, full dump (MMSXLIT), sync close
    -> MPRO sync close
    -> EEVT SetClientType "TYPE:StageMix SYNCDIR:2"
    -> MMIX subscribe "Mixing", hash compare, sync open
    <- MMIX ..., full dump of every mix parameter, sync close   (parsed: EQ)
    -> MMIX sync close
    <- MMIX set-param  (a change on the desk / another app / scene recall)
    -> MPRC subscribe "Processing", ...       then each other function in tf5.functions, one at a time
    <-> EEVT KeepAlive

Modules:

- codec:  the wire format (elements, messages, frames) and the few messages we build
- guard:  what may be sent (read-only by default); every outgoing frame is checked
- dump:   a function's parameter tree / data image, and a channel's EQ
- tree:   the TF5's V4.55 tree (tf5_v455.json): frame tags, descriptor MD5s, each parameter's schema
- client: TFEditorClient, the session (connect, sync, keepalive, user-requested writes)

Run ``python -m tf_daemon.console.editor <console-ip>`` to print the EQ (read-only).
"""
