"""The Yamaha TF5 console over the network.

- patch:  which console source each USB capture channel carries
- rcp:    names, faders, sends... over the remote control protocol (TCP 49280)
- editor: the EQ over the TF Editor protocol (TCP 50368), written only on request
- module: TF5Module, all of it in the app
- sim:    a simulated console (demo mode, tests)
"""
