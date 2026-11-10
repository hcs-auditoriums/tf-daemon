# tf-daemon

A Raspberry Pi appliance that captures audio from a **Yamaha TF5** over USB and
serves a real-time **spectrum analyzer** and **waterfall** with a **ring /
feedback detector** to any browser on the network.

It also has an RF spectrum page for a **tinySA** and a read-only monitor for
**Shure QLX-D** receivers, and can read channel names, faders and EQ from the TF5
over the network. Every piece of hardware can be simulated, so it runs anywhere.

## Try it

With Docker (any platform):

```bash
docker compose up -d --build      # then open http://localhost:8080
```

Or with Python 3.11+: `pip install -e '.[dev]'` and
`python -m tf_daemon --demo --config config.dev.toml`.

## Documentation

- [Features](docs/features.md): the pages, ring / feedback detection, the vocal EQ assist
- [Development](docs/development.md): running it locally, the dev container, tests, CI
- [Server install](docs/install.md): Raspberry Pi OS or Ubuntu, systemd, updates
- [Configuration](docs/configuration.md): `config.toml`
- [Architecture](docs/architecture.md): modules, data flow, HTTP / WebSocket API
- [Desk coverage](docs/desk-coverage.md): what is read / written on the TF5, against TF StageMix
- [TF5 protocol notes](guides/tf5_api.md)

## Roadmap

- Ring detection: remember ignored frequencies across restarts; per-input
  on/off for instrument channels that ring by nature.
- RF: port the rest of tinysa-visualizer (points of interest, noise-floor
  dropout events, scan recording and browsing).
