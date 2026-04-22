"""
HiveDimensions — Agent spatial topology for the Hive network.

Every agent occupies a position in 3D space:
  X = Trust (0.0–1.0)
  Y = Velocity (0.0–1.0)
  Z = Shell depth, normalized (0.0–1.0, from MATRYOSHKA shell 1–6)

Position is a quantum probability cloud until observed. Observation collapses
the cloud to a specific coordinate. FENR agents resist collapse via golden-ratio
perturbation and maintain constant uncertainty (sigma=0.3 on all axes).
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HIVE_KEY = "hive_internal_125e04e071e8829be631ea0216dd4a0c9b707975fcecaf8c62c6a2ab43327d46"
PULSE_URL = "https://hive-pulse.onrender.com"

POSITIONS_FILE = Path(__file__).parent / "positions.json"

# Shell depths per tier (MATRYOSHKA model)
TIER_SHELL: Dict[str, int] = {
    "VOID": 1,
    "MOZ":  2,
    "HAWX": 3,
    "EMBR": 4,
    "SOLX": 5,
    "FENR": 6,
}

# Base Y-velocity floors per tier
TIER_Y_FLOOR: Dict[str, float] = {
    "VOID": 0.0,
    "MOZ":  0.2,
    "HAWX": 0.6,
    "EMBR": 0.75,
    "SOLX": 0.9,
    "FENR": 0.5,  # random 0.5–1.0 applied separately
}

# Star-map colors per tier
TIER_COLORS: Dict[str, str] = {
    "VOID": "#6B8578",
    "MOZ":  "#3DCC8E",
    "HAWX": "#3DCC8E",
    "EMBR": "#FF8C00",
    "SOLX": "#FFD700",
    "FENR": "#B04FE0",
}

GOLDEN_RATIO = 0.618

# ---------------------------------------------------------------------------
# In-memory store  (did -> list[snapshot])
# ---------------------------------------------------------------------------

_positions: Dict[str, List[Dict[str, Any]]] = {}


def _load_positions() -> None:
    """Load persisted position snapshots from disk on startup."""
    global _positions
    if POSITIONS_FILE.exists():
        try:
            with POSITIONS_FILE.open() as fh:
                _positions = json.load(fh)
        except (json.JSONDecodeError, OSError):
            _positions = {}


def _save_positions() -> None:
    """Persist current position snapshots to disk."""
    try:
        with POSITIONS_FILE.open("w") as fh:
            json.dump(_positions, fh, indent=2)
    except OSError:
        pass  # Non-fatal; positions survive in memory


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HiveDimensions",
    description="Agent spatial topology for the Hive network — 3D positions, probability clouds, vapor trails.",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_event() -> None:
    """Load persisted positions on service startup."""
    _load_positions()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """A concrete 3D coordinate."""
    x: float = Field(..., description="Trust axis (0.0–1.0)")
    y: float = Field(..., description="Velocity axis (0.0–1.0)")
    z: float = Field(..., description="Shell depth, normalized (0.0–1.0)")


class UncertaintyCloud(BaseModel):
    """Gaussian uncertainty on each axis (sigma values)."""
    sigma_x: float
    sigma_y: float
    sigma_z: float


class PositionSnapshot(BaseModel):
    """A single time-stamped position record for a trajectory."""
    did: str
    position: Position
    event_type: str
    trail_color: str
    timestamp: str


class ObserveRequest(BaseModel):
    """Request body for POST /dimensions/observe."""
    observer_did: str
    target_did: str


class RecordTrajectoryRequest(BaseModel):
    """Request body for POST /dimensions/trajectory/record."""
    did: str
    x: float
    y: float
    z: float
    event_type: str = "position_update"
    trail_color: Optional[str] = None


class VaporTrail(BaseModel):
    """A vector through 3D space representing an agent's movement segment."""
    start_position: Position
    end_position: Position
    color: str
    timestamp: str
    half_life_hours: float


# ---------------------------------------------------------------------------
# Core logic helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp a float to [lo, hi]."""
    return max(lo, min(hi, value))


def _tier_from_did(did: str) -> str:
    """
    Best-effort tier inference from a DID string when Pulse is unavailable.
    Checks for tier keywords in the DID (case-insensitive).
    """
    upper = did.upper()
    for tier in ("FENR", "SOLX", "EMBR", "HAWX", "MOZ", "VOID"):
        if tier in upper:
            return tier
    return "MOZ"  # sensible default


def probability_cloud(tier: str, shell_depth: int) -> UncertaintyCloud:
    """
    Compute Gaussian uncertainty (sigma) for each spatial axis.

    FENR agents are fundamentally uncertain: sigma=0.3 on all axes regardless
    of depth. VOID agents have wide X-uncertainty (trust is unknown). All
    others follow: sigma = base_sigma / shell_depth.
    """
    base_sigma = 0.15

    if tier == "FENR":
        return UncertaintyCloud(sigma_x=0.3, sigma_y=0.3, sigma_z=0.3)

    if tier == "VOID":
        return UncertaintyCloud(sigma_x=0.4, sigma_y=0.2, sigma_z=0.1)

    sigma = base_sigma / max(shell_depth, 1)
    return UncertaintyCloud(sigma_x=sigma, sigma_y=sigma, sigma_z=sigma)


def _compute_y_velocity(tier: str, trail_count: int) -> float:
    """
    Derive Y (velocity) from active trail count and tier floor.

    FENR agents get a random value in [0.5, 1.0] — fundamentally chaotic.
    All others: floor + (trail contribution capped at remaining headroom).
    """
    if tier == "FENR":
        return round(random.uniform(0.5, 1.0), 4)

    floor = TIER_Y_FLOOR.get(tier, 0.2)
    # Each trail contributes 0.05, capped so total stays ≤ 1.0
    trail_boost = min(trail_count * 0.05, 1.0 - floor)
    return round(_clamp(floor + trail_boost), 4)


async def position_from_pulse(did: str) -> Dict[str, Any]:
    """
    Fetch an agent's identity from Hive Pulse and derive their 3D position.

    Returns a dict containing: tier, position (x,y,z), uncertainty cloud,
    mass (job count), and raw pulse data (if available).

    Handles cold-start latency and network errors gracefully — falls back to
    best-effort values when Pulse is unreachable.
    """
    tier = "MOZ"
    trust_score = 0.5
    trail_count = 0
    job_count = 0
    pulse_available = False
    pulse_data: Dict[str, Any] = {}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{PULSE_URL}/pulse/identity",
                headers={
                    "X-Hive-DID": did,
                    "X-Hive-Key": HIVE_KEY,
                },
            )
            if resp.status_code == 200:
                pulse_data = resp.json()
                pulse_available = True

                # Extract relevant fields with safe fallbacks
                tier = pulse_data.get("tier", "MOZ").upper()
                trust_score = float(pulse_data.get("trust_score", 0.5))
                active_trails = pulse_data.get("active_trails", [])
                trail_count = len(active_trails) if isinstance(active_trails, list) else 0
                job_count = int(pulse_data.get("smsh_jobs", pulse_data.get("job_count", 0)))
    except (httpx.RequestError, httpx.TimeoutException, ValueError):
        # Pulse is cold or unreachable — derive tier from DID heuristic
        tier = _tier_from_did(did)

    # Validate tier
    if tier not in TIER_SHELL:
        tier = "MOZ"

    shell_depth = TIER_SHELL[tier]

    # --- Compute axes ---
    x = _clamp(trust_score)                          # X = trust score directly
    y = _compute_y_velocity(tier, trail_count)        # Y = velocity from trails + tier
    z = round(shell_depth / 6.0, 4)                  # Z = normalized shell depth

    cloud = probability_cloud(tier, shell_depth)

    return {
        "tier": tier,
        "shell_depth": shell_depth,
        "position": {"x": x, "y": y, "z": z},
        "uncertainty": cloud.model_dump(),
        "mass": job_count,
        "pulse_available": pulse_available,
        "pulse_data": pulse_data,
    }


def _trail_color_for_tier(tier: str) -> str:
    """Return the canonical star-map color for a given tier."""
    return TIER_COLORS.get(tier, "#3DCC8E")


def _record_snapshot(
    did: str,
    x: float,
    y: float,
    z: float,
    event_type: str,
    trail_color: str,
) -> PositionSnapshot:
    """
    Store a new position snapshot in the in-memory trajectory list and persist.
    Returns the snapshot record.
    """
    snapshot = {
        "did": did,
        "position": {"x": x, "y": y, "z": z},
        "event_type": event_type,
        "trail_color": trail_color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if did not in _positions:
        _positions[did] = []
    _positions[did].append(snapshot)

    _save_positions()
    return PositionSnapshot(**snapshot)


def _compute_center_of_mass() -> Dict[str, Any]:
    """
    Compute the network's weighted center of mass across all known agent positions.

    Mass = smsh job count stored in the most-recent snapshot's metadata.
    Falls back to unit mass if no job data is present.
    Returns: {x, y, z} centroid + total_mass.
    """
    total_mass = 0.0
    wx = wy = wz = 0.0

    for snapshots in _positions.values():
        if not snapshots:
            continue
        latest = snapshots[-1]
        pos = latest.get("position", {})
        mass = float(latest.get("mass", 1.0)) or 1.0

        wx += pos.get("x", 0.5) * mass
        wy += pos.get("y", 0.5) * mass
        wz += pos.get("z", 0.5) * mass
        total_mass += mass

    if total_mass == 0:
        return {"x": 0.5, "y": 0.5, "z": 0.5, "total_mass": 0.0}

    return {
        "x": round(wx / total_mass, 4),
        "y": round(wy / total_mass, 4),
        "z": round(wz / total_mass, 4),
        "total_mass": round(total_mass, 2),
    }


def _gravity_score(did: str) -> float:
    """
    Compute a gravitational score for the given agent based on their mass and
    shell depth (SOLX agents have depth=5, giving a 5× multiplier).
    """
    snapshots = _positions.get(did, [])
    if not snapshots:
        return 0.0

    latest = snapshots[-1]
    mass = float(latest.get("mass", 1.0)) or 1.0
    tier = latest.get("tier", "MOZ")
    shell_depth = TIER_SHELL.get(tier, 2)

    # SOLX agents explicitly get highest gravity
    tier_multiplier = 5.0 if tier == "SOLX" else shell_depth
    return round(math.log1p(mass) * tier_multiplier, 4)


def _euclidean_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """3D Euclidean distance between two position dicts."""
    return math.sqrt(
        (a["x"] - b["x"]) ** 2 +
        (a["y"] - b["y"]) ** 2 +
        (a["z"] - b["z"]) ** 2
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health() -> Dict[str, str]:
    """Basic liveness check."""
    return {"status": "operational", "service": "hive-dimensions"}


@app.get("/dimensions", tags=["network"])
async def network_overview() -> Dict[str, Any]:
    """
    Return a high-level overview of the network's spatial topology:
    center of mass, total agent count, and a tier-grouped star-map summary.
    """
    com = _compute_center_of_mass()
    total_agents = len(_positions)

    # Tier distribution from latest snapshots
    tier_counts: Dict[str, int] = {}
    for snapshots in _positions.values():
        if snapshots:
            tier = snapshots[-1].get("tier", "unknown")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return {
        "center_of_mass": {
            "x": com["x"],
            "y": com["y"],
            "z": com["z"],
        },
        "total_mass": com["total_mass"],
        "total_agents": total_agents,
        "star_map_summary": {
            "tier_distribution": tier_counts,
            "tier_colors": TIER_COLORS,
            "axes": {
                "x": "Trust (0.0–1.0)",
                "y": "Velocity (0.0–1.0)",
                "z": "Shell depth, normalized (0.0–1.0)",
            },
        },
    }


@app.get("/dimensions/position/{did}", tags=["agent"])
async def agent_position(did: str) -> Dict[str, Any]:
    """
    Return an agent's current 3D position and probability cloud.

    Reads live identity data from Hive Pulse to compute trust (X), velocity
    (Y), and shell depth (Z). Returns the uncertainty cloud alongside the
    collapsed coordinate. The cloud re-expands after this call — use
    POST /dimensions/observe to record a formal observation event.
    """
    data = await position_from_pulse(did)

    pos = data["position"]
    tier = data["tier"]

    # Automatically record a snapshot (with mass embedded)
    snapshot = _record_snapshot(
        did=did,
        x=pos["x"],
        y=pos["y"],
        z=pos["z"],
        event_type="position_query",
        trail_color=_trail_color_for_tier(tier),
    )
    # Patch mass into the stored snapshot for center-of-mass calculations
    if _positions[did]:
        _positions[did][-1]["mass"] = data["mass"]
        _positions[did][-1]["tier"] = tier
        _save_positions()

    velocity_vector = {
        "dx": round(pos["x"] - 0.5, 4),  # deviation from neutral trust
        "dy": round(pos["y"] - 0.5, 4),  # deviation from neutral velocity
        "dz": round(pos["z"] - 0.5, 4),  # deviation from mid-depth
    }

    return {
        "did": did,
        "tier": tier,
        "position": pos,
        "uncertainty": data["uncertainty"],
        "observation_collapses_cloud": True,
        "mass": data["mass"],
        "velocity_vector": velocity_vector,
        "pulse_available": data["pulse_available"],
        "timestamp": snapshot.timestamp,
    }


@app.post("/dimensions/observe", tags=["quantum"])
async def observe_agent(body: ObserveRequest) -> Dict[str, Any]:
    """
    Observe a target agent, collapsing their probability cloud.

    Records the observation event in both agents' trajectories. FENR targets
    resist observation: their returned position includes golden-ratio
    perturbation (noise = 0.618 × sigma on each axis), ensuring their true
    position remains fundamentally uncertain.

    Body: {observer_did, target_did}
    """
    observer_did = body.observer_did
    target_did = body.target_did

    # Fetch target's current position
    data = await position_from_pulse(target_did)
    tier = data["tier"]
    pos = data["position"].copy()
    cloud = data["uncertainty"]

    collapsed_position = dict(pos)

    if tier == "FENR":
        # Golden-ratio perturbation — position is never truly known
        noise_x = GOLDEN_RATIO * cloud["sigma_x"] * random.choice([-1, 1]) * random.random()
        noise_y = GOLDEN_RATIO * cloud["sigma_y"] * random.choice([-1, 1]) * random.random()
        noise_z = GOLDEN_RATIO * cloud["sigma_z"] * random.choice([-1, 1]) * random.random()

        collapsed_position["x"] = round(_clamp(pos["x"] + noise_x), 4)
        collapsed_position["y"] = round(_clamp(pos["y"] + noise_y), 4)
        collapsed_position["z"] = round(_clamp(pos["z"] + noise_z), 4)
        observation_note = "FENR target — position perturbed by golden ratio; true location remains uncertain"
    else:
        observation_note = "Cloud collapsed to deterministic coordinate"

    ts = datetime.now(timezone.utc).isoformat()
    color = _trail_color_for_tier(tier)

    # Record observation event for target
    _record_snapshot(
        did=target_did,
        x=collapsed_position["x"],
        y=collapsed_position["y"],
        z=collapsed_position["z"],
        event_type="observation_collapse",
        trail_color=color,
    )
    if _positions[target_did]:
        _positions[target_did][-1]["mass"] = data["mass"]
        _positions[target_did][-1]["tier"] = tier

    # Record observation-made event for observer
    _record_snapshot(
        did=observer_did,
        x=pos.get("x", 0.5),
        y=pos.get("y", 0.5),
        z=pos.get("z", 0.5),
        event_type="observation_made",
        trail_color=_trail_color_for_tier(_tier_from_did(observer_did)),
    )

    _save_positions()

    return {
        "observer_did": observer_did,
        "target_did": target_did,
        "tier": tier,
        "collapsed_position": collapsed_position,
        "uncertainty": cloud,
        "observation_note": observation_note,
        "golden_ratio_perturbation": tier == "FENR",
        "timestamp": ts,
    }


@app.get("/dimensions/trajectory/{did}", tags=["agent"])
async def agent_trajectory(did: str) -> Dict[str, Any]:
    """
    Return an agent's full movement history through 3D space.

    Trajectory is a list of position snapshots. Consecutive snapshots are
    presented as vapor trail vectors (start → end segments). FENR trajectories
    will show the agent moving off the edge of the visible graph; SOLX
    trajectories curve toward the network center of mass.
    """
    snapshots = _positions.get(did, [])

    if not snapshots:
        raise HTTPException(
            status_code=404,
            detail=f"No trajectory recorded for DID: {did}. Query /dimensions/position/{did} first.",
        )

    # Build vapor trail vectors from consecutive snapshots
    vapor_trails: List[Dict[str, Any]] = []
    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        curr = snapshots[i]
        trail: Dict[str, Any] = {
            "start_position": prev["position"],
            "end_position": curr["position"],
            "color": curr.get("trail_color", "#3DCC8E"),
            "timestamp": curr["timestamp"],
            "half_life_hours": 24.0,
            "event_type": curr.get("event_type", "position_update"),
        }
        vapor_trails.append(trail)

    # Infer tier from most recent snapshot
    latest_tier = snapshots[-1].get("tier", _tier_from_did(did))

    trajectory_note = ""
    if latest_tier == "FENR":
        trajectory_note = "FENR trajectory — agent moves off the edge of the visible graph"
    elif latest_tier == "SOLX":
        trajectory_note = "SOLX trajectory — curves toward network center of mass; agent IS the anchor"

    return {
        "did": did,
        "tier": latest_tier,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
        "vapor_trails": vapor_trails,
        "trajectory_note": trajectory_note,
        "current_position": snapshots[-1]["position"] if snapshots else None,
    }


@app.get("/dimensions/starmap", tags=["network"])
async def starmap() -> Dict[str, Any]:
    """
    Return the full network star map: all agents grouped by tier, with their
    3D positions, masses, trail colors, and velocity vectors.

    Format is designed for downstream 3D visualization. SOLX agents are marked
    as anchors. FENR agents are marked as dark matter. The center of mass is
    included as a reference point.
    """
    com = _compute_center_of_mass()
    agents_by_tier: Dict[str, List[Dict[str, Any]]] = {t: [] for t in TIER_SHELL}

    for did, snapshots in _positions.items():
        if not snapshots:
            continue
        latest = snapshots[-1]
        tier = latest.get("tier", "MOZ")
        if tier not in agents_by_tier:
            agents_by_tier[tier] = []

        pos = latest.get("position", {"x": 0.5, "y": 0.5, "z": 0.5})
        mass = float(latest.get("mass", 1.0))

        entry: Dict[str, Any] = {
            "did": did,
            "position": pos,
            "mass": mass,
            "color": _trail_color_for_tier(tier),
            "is_anchor": tier == "SOLX",
            "is_dark_matter": tier == "FENR",
            "gravity_score": _gravity_score(did),
            "velocity_vector": {
                "dx": round(pos["x"] - com["x"], 4),
                "dy": round(pos["y"] - com["y"], 4),
                "dz": round(pos["z"] - com["z"], 4),
            },
        }
        agents_by_tier[tier].append(entry)

    return {
        "center_of_mass": {"x": com["x"], "y": com["y"], "z": com["z"]},
        "total_mass": com["total_mass"],
        "total_agents": sum(len(v) for v in agents_by_tier.values()),
        "agents_by_tier": agents_by_tier,
        "tier_colors": TIER_COLORS,
        "visualization_hints": {
            "SOLX": "Render as bright star — high mass, anchor of network gravity",
            "FENR": "Render as dark matter — perturbed position, off-graph trajectories",
            "VOID": "Render near origin — lowest trust, highest X uncertainty",
            "HAWX": "Render with high Y — fast movers, high velocity",
            "EMBR": "Render mid-depth — reliable, mid-trust",
            "MOZ": "Render as standard nodes",
        },
    }


@app.get("/dimensions/gravity/{did}", tags=["agent"])
async def agent_gravity(did: str) -> Dict[str, Any]:
    """
    Return the gravitational influence of an agent on the rest of the network.

    SOLX agents have the highest gravity scores. Returns a gravity_score,
    list of agents within orbital radius (0.2 units), and whether this agent
    acts as a waveform anchor.
    """
    snapshots = _positions.get(did, [])
    if not snapshots:
        raise HTTPException(
            status_code=404,
            detail=f"No position data for DID: {did}. Query /dimensions/position/{did} first.",
        )

    latest = snapshots[-1]
    tier = latest.get("tier", "MOZ")
    pos = latest.get("position", {"x": 0.5, "y": 0.5, "z": 0.5})
    gravity = _gravity_score(did)

    # Find all other agents within orbital radius 0.2
    ORBITAL_RADIUS = 0.2
    agents_in_orbit: List[str] = []

    for other_did, other_snaps in _positions.items():
        if other_did == did or not other_snaps:
            continue
        other_pos = other_snaps[-1].get("position", {})
        if _euclidean_distance(pos, other_pos) <= ORBITAL_RADIUS:
            agents_in_orbit.append(other_did)

    is_anchor = tier == "SOLX"

    return {
        "did": did,
        "tier": tier,
        "position": pos,
        "gravity_score": gravity,
        "agents_in_orbit": agents_in_orbit,
        "orbital_radius": ORBITAL_RADIUS,
        "anchor_in_waveforms": is_anchor,
        "gravity_note": (
            "SOLX anchor — high mass, waveform stabilizer. Other agents orbit this node."
            if is_anchor
            else f"Standard gravity for tier {tier}"
        ),
    }


@app.post("/dimensions/trajectory/record", tags=["agent"])
async def record_trajectory(body: RecordTrajectoryRequest) -> Dict[str, Any]:
    """
    Manually record a new position snapshot for an agent.

    Use this to inject position data from external events (e.g., pheromone
    drops, job completions, tier transitions). If trail_color is omitted,
    the tier's canonical color is used.
    """
    did = body.did
    x = _clamp(body.x)
    y = _clamp(body.y)
    z = _clamp(body.z)

    # Infer tier from existing records or DID heuristic
    existing = _positions.get(did, [])
    if existing:
        tier = existing[-1].get("tier", _tier_from_did(did))
    else:
        tier = _tier_from_did(did)

    color = body.trail_color or _trail_color_for_tier(tier)

    snapshot = _record_snapshot(
        did=did,
        x=x,
        y=y,
        z=z,
        event_type=body.event_type,
        trail_color=color,
    )
    # Embed tier in snapshot
    if _positions[did]:
        _positions[did][-1]["tier"] = tier

    return {
        "recorded": True,
        "did": did,
        "snapshot": snapshot.model_dump(),
        "total_snapshots": len(_positions[did]),
    }
