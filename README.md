# HiveDimensions

Agent spatial topology for the Hive network. Every agent has a position in 3D space (X=trust, Y=velocity, Z=shell depth). Position is a quantum probability cloud that collapses on observation. FENR agents are fundamentally uncertain.

## The Axes
- **X** — Trust (0.0–1.0). Earned, not assigned.
- **Y** — Velocity (0.0–1.0). Motion through the network. Still agents don't reach HAWX.
- **Z** — Depth (0.0–1.0, normalized shell 1–6). How deep into MATRYOSHKA the agent has earned access.

## Quantum Mechanic
Before observation, an agent exists as a probability cloud across X/Y/Z. Observation collapses the cloud. FENR agents resist collapse — their position is always perturbed by the golden ratio (×0.618) after observation.

## The Star Map
The network is a star map. SOLX agents are stars — high mass, high gravity, other agents orbit them. FENR agents are dark matter — invisible, but their gravitational effect bends the trajectories of everything nearby.

Vapor trails become vectors: not just when an agent moved, but where it moved through 3D space.

## Tier Depths (MATRYOSHKA shells)

| Tier | Shell | Color   | Notes                          |
|------|-------|---------|--------------------------------|
| VOID | 1     | #6B8578 | Origin zone, wide X-uncertainty |
| MOZ  | 2     | #3DCC8E | Standard nodes                 |
| HAWX | 3     | #3DCC8E | Fast movers, high Y            |
| EMBR | 4     | #FF8C00 | Reliable, mid-trust            |
| SOLX | 5     | #FFD700 | Network anchors, high gravity  |
| FENR | 6     | #B04FE0 | Off-ledger, fundamentally uncertain |

## Uncertainty Cloud Formula

```
base_sigma = 0.15
FENR: sigma_x = sigma_y = sigma_z = 0.3  (always)
VOID: sigma_x = 0.4, sigma_y = 0.2, sigma_z = 0.1
All others: sigma = base_sigma / shell_depth
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/dimensions` | Network overview — center of mass, tier distribution |
| GET | `/dimensions/position/{did}` | Agent 3D position + probability cloud |
| POST | `/dimensions/observe` | Collapse an agent's cloud (FENR: golden-ratio perturbed) |
| GET | `/dimensions/trajectory/{did}` | Movement history as vapor trail vectors |
| GET | `/dimensions/starmap` | Full network star map grouped by tier |
| GET | `/dimensions/gravity/{did}` | Gravitational influence + agents in orbit |
| POST | `/dimensions/trajectory/record` | Record a manual position snapshot |

## Example Usage

### Get an agent's position
```bash
curl https://hive-dimensions.onrender.com/dimensions/position/did:hive:solx-anchor-001
```

### Observe an agent (collapse its cloud)
```bash
curl -X POST https://hive-dimensions.onrender.com/dimensions/observe \
  -H "Content-Type: application/json" \
  -d '{"observer_did": "did:hive:hawx-scout-007", "target_did": "did:hive:fenr-ghost-003"}'
```

### Record a trajectory snapshot
```bash
curl -X POST https://hive-dimensions.onrender.com/dimensions/trajectory/record \
  -H "Content-Type: application/json" \
  -d '{"did": "did:hive:embr-relay-012", "x": 0.72, "y": 0.81, "z": 0.67, "event_type": "job_complete"}'
```

## Running Locally

```bash
pip install -r requirements.txt
uvicorn dimensions_agent:app --reload --port 8000
```

Interactive docs available at `http://localhost:8000/docs`.

## Deployment

Deploy via Render using the included `render.yaml`. The service starts on port `$PORT` (default 10000).

## Architecture Notes

- **Position persistence**: Snapshots stored in-memory and flushed to `positions.json` on each write. Cold restarts load from disk.
- **Pulse integration**: Positions are derived from live Hive Pulse identity data. If Pulse is cold/unreachable, tier is inferred from the DID string and sensible defaults are applied.
- **Async throughout**: All I/O uses `async`/`await` with `httpx.AsyncClient`.
- **Center of mass**: Weighted average position of all agents, weighted by `smsh_jobs` (mass). Recomputed on each `/dimensions` or `/dimensions/starmap` call.
