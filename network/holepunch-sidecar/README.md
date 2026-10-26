# Holepunch sidecar

Optional peer-to-peer sidecar for discovery and artifact distribution. It does not
replace the Python coordinator; aggregation and the outer optimizer remain in
`diloco/`.

## Pieces

- Hyperswarm: peer discovery and NAT traversal.
- Hypercore: append-only ledger for round metadata.
- Hyperdrive: replication of checkpoint and adapter directories.
- HyperDB: metadata index for artifacts.

## Install

```bash
cd network/holepunch-sidecar
npm install
npm run build:schema
```

## Commands

```bash
node index.js --mode announce --topic diloco-run --name node-a --endpoint http://127.0.0.1:8000
node index.js --mode ledger --storage ./ledger --message "round 0"
node index.js --mode ledger --storage ./ledger
node index.js --mode share --storage ./drive --dir ./checkpoint --topic diloco-run
node index.js --mode index --storage ./index --key run --value demo
node index.js --mode index --storage ./index
```

`announce` joins the swarm and prints peer events. `share` publishes a directory
through Hyperdrive and then announces its discovery key. `ledger` stores round
records. `index` stores artifact metadata.

## Integration

Use the sidecar next to `run_coordinator.py` and `run_worker.py`: the Python
processes keep the DiLoCo protocol, while the sidecar handles peer discovery and
artifact replication. For WAN tests, put `scripts/wan_emulate.py` between the
worker and coordinator or use `tc netem` on the host.
