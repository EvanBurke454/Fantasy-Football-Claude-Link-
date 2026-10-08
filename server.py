"""Sleeper fantasy football MCP server (read-only, uses Sleeper's public API)."""
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE = "https://api.sleeper.app/v1"
PLAYER_TTL = 24 * 60 * 60  # Sleeper asks for at most one players call per day

mcp = FastMCP(
    "Sleeper",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    # Needed when hosted behind a public domain (Render, Fly, Railway, etc.)
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_players: dict = {}
_players_loaded_at = 0.0


async def _get(path: str):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE}{path}")
        r.raise_for_status()
        return r.json()


async def _player_names() -> dict:
    """Map player_id -> 'Name (POS, TEAM)'. Cached for 24h."""
    global _players, _players_loaded_at
    if not _players or time.time() - _players_loaded_at > PLAYER_TTL:
        raw = await _get("/players/nfl")
        _players = {
            pid: (
                f"{p.get('full_name') or pid} "
                f"({p.get('position') or '?'}, {p.get('team') or 'FA'})"
                + (f" [{p['injury_status']}]" if p.get("injury_status") else "")
            )
            for pid, p in raw.items()
        }
        _players_loaded_at = time.time()
    return _players


def _names(ids, table):
    return [table.get(i, i) for i in (ids or [])]


@mcp.tool()
async def find_leagues(username: str, season: str = "2026") -> list:
    """List a user's NFL fantasy leagues (name + league_id) from their Sleeper username."""
    user = await _get(f"/user/{username}")
    leagues = await _get(f"/user/{user['user_id']}/leagues/nfl/{season}")
    return [{"league_id": l["league_id"], "name": l["name"], "status": l["status"]} for l in leagues]


@mcp.tool()
async def get_league(league_id: str) -> dict:
    """League settings: name, season, status, roster positions, scoring settings, playoff info."""
    return await _get(f"/league/{league_id}")


@mcp.tool()
async def get_standings(league_id: str) -> list:
    """Standings sorted by wins, then points for. Includes team name, owner, record, points."""
    users = {u["user_id"]: u for u in await _get(f"/league/{league_id}/users")}
    rows = []
    for r in await _get(f"/league/{league_id}/rosters"):
        owner = users.get(r.get("owner_id"), {})
        s = r.get("settings", {})
        rows.append({
            "roster_id": r["roster_id"],
            "team": (owner.get("metadata") or {}).get("team_name") or owner.get("display_name"),
            "owner": owner.get("display_name"),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "points_for": s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
            "points_against": s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100,
        })
    rows.sort(key=lambda x: (-x["wins"], -x["points_for"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


@mcp.tool()
async def get_rosters(league_id: str) -> list:
    """Every team's roster with real player names, starters, bench, and IR."""
    users = {u["user_id"]: u for u in await _get(f"/league/{league_id}/users")}
    table = await _player_names()
    out = []
    for r in await _get(f"/league/{league_id}/rosters"):
        owner = users.get(r.get("owner_id"), {})
        starters = r.get("starters") or []
        all_players = r.get("players") or []
        out.append({
            "roster_id": r["roster_id"],
            "team": (owner.get("metadata") or {}).get("team_name") or owner.get("display_name"),
            "owner": owner.get("display_name"),
            "starters": _names(starters, table),
            "bench": _names([p for p in all_players if p not in starters and p not in (r.get("reserve") or [])], table),
            "injured_reserve": _names(r.get("reserve"), table),
        })
    return out


@mcp.tool()
async def get_matchups(league_id: str, week: int) -> list:
    """Matchups and scores for a given week, paired by matchup_id, with player names."""
    users = {u["user_id"]: u for u in await _get(f"/league/{league_id}/users")}
    rosters = {r["roster_id"]: r for r in await _get(f"/league/{league_id}/rosters")}
    table = await _player_names()
    games: dict = {}
    for m in await _get(f"/league/{league_id}/matchups/{week}"):
        owner = users.get(rosters.get(m["roster_id"], {}).get("owner_id"), {})
        games.setdefault(m.get("matchup_id"), []).append({
            "team": (owner.get("metadata") or {}).get("team_name") or owner.get("display_name"),
            "points": m.get("points"),
            "starters": _names(m.get("starters"), table),
        })
    return [{"matchup_id": k, "teams": v} for k, v in games.items()]


@mcp.tool()
async def get_transactions(league_id: str, week: int) -> list:
    """Trades, waiver claims, and free-agent moves for a given week, with player names."""
    rosters = {r["roster_id"]: r for r in await _get(f"/league/{league_id}/rosters")}
    users = {u["user_id"]: u["display_name"] for u in await _get(f"/league/{league_id}/users")}
    table = await _player_names()

    def who(rid):
        return users.get(rosters.get(rid, {}).get("owner_id"), f"roster {rid}")

    out = []
    for t in await _get(f"/league/{league_id}/transactions/{week}"):
        out.append({
            "type": t["type"],
            "status": t["status"],
            "added": {table.get(p, p): who(r) for p, r in (t.get("adds") or {}).items()},
            "dropped": {table.get(p, p): who(r) for p, r in (t.get("drops") or {}).items()},
        })
    return out


@mcp.tool()
async def get_trending_players(kind: str = "add", limit: int = 25) -> list:
    """Most added ('add') or dropped ('drop') NFL players on Sleeper over the last 24 hours."""
    table = await _player_names()
    data = await _get(f"/players/nfl/trending/{kind}?lookback_hours=24&limit={limit}")
    return [{"player": table.get(d["player_id"], d["player_id"]), "count": d["count"]} for d in data]


@mcp.tool()
async def get_nfl_state() -> dict:
    """Current NFL week, season, and season type."""
    return await _get("/state/nfl")


# ---------------------------------------------------------------------------
# FantasyCalc trade values (public endpoint; unverified from build sandbox)
# ---------------------------------------------------------------------------
FC_URL = "https://api.fantasycalc.com/values/current"
_fc_cache: dict = {}
FC_TTL = 60 * 60  # 1 hour


async def _fc_values(dynasty: bool, num_qbs: int, num_teams: int, ppr: float) -> list:
    key = (dynasty, num_qbs, num_teams, ppr)
    hit = _fc_cache.get(key)
    if hit and time.time() - hit[0] < FC_TTL:
        return hit[1]
    params = {
        "isDynasty": str(dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(FC_URL, params=params)
        r.raise_for_status()
        data = r.json()
    _fc_cache[key] = (time.time(), data)
    return data


def _fc_row(e: dict) -> dict:
    p = e.get("player", {})
    return {
        "name": p.get("name"),
        "position": p.get("position"),
        "team": p.get("maybeTeam"),
        "age": p.get("maybeAge"),
        "value": e.get("value"),
        "overall_rank": e.get("overallRank"),
        "position_rank": e.get("positionRank"),
        "trend_30_day": e.get("trend30Day"),
        "sleeper_id": p.get("sleeperId"),
    }


@mcp.tool()
async def get_trade_values(
    dynasty: bool = False,
    num_qbs: int = 1,
    num_teams: int = 12,
    ppr: float = 1.0,
    position: str = "",
    limit: int = 50,
) -> list:
    """FantasyCalc trade values/rankings. dynasty=False for redraft, num_qbs=2 for superflex,
    ppr is 0, 0.5 or 1. Optional position filter (QB/RB/WR/TE). Higher value = more valuable."""
    rows = [_fc_row(e) for e in await _fc_values(dynasty, num_qbs, num_teams, ppr)]
    if position:
        rows = [r for r in rows if (r["position"] or "").upper() == position.upper()]
    rows.sort(key=lambda r: r["overall_rank"] or 9999)
    return rows[:limit]


@mcp.tool()
async def lookup_player_values(
    names: list[str],
    dynasty: bool = False,
    num_qbs: int = 1,
    num_teams: int = 12,
    ppr: float = 1.0,
) -> list:
    """Look up FantasyCalc values for specific players by name (partial, case-insensitive).
    Handy for evaluating trades: compare the summed values of each side."""
    rows = [_fc_row(e) for e in await _fc_values(dynasty, num_qbs, num_teams, ppr)]
    out = []
    for n in names:
        matches = [r for r in rows if n.lower() in (r["name"] or "").lower()]
        out.append({"query": n, "matches": matches[:3]})
    return out


@mcp.tool()
async def get_roster_values(league_id: str, dynasty: bool = False) -> list:
    """Every team's players with FantasyCalc values, matched by Sleeper ID. Settings (superflex,
    PPR, team count) are read from the Sleeper league automatically. Teams are ranked by total value."""
    league = await _get(f"/league/{league_id}")
    positions = league.get("roster_positions") or []
    num_qbs = 2 if ("SUPER_FLEX" in positions or positions.count("QB") >= 2) else 1
    rec = (league.get("scoring_settings") or {}).get("rec", 0)
    ppr = 1.0 if rec >= 1 else 0.5 if rec >= 0.5 else 0.0
    num_teams = league.get("total_rosters") or 12

    values = {
        str(r["sleeper_id"]): r
        for r in (_fc_row(e) for e in await _fc_values(dynasty, num_qbs, num_teams, ppr))
        if r["sleeper_id"]
    }
    users = {u["user_id"]: u for u in await _get(f"/league/{league_id}/users")}
    table = await _player_names()
    teams = []
    for r in await _get(f"/league/{league_id}/rosters"):
        owner = users.get(r.get("owner_id"), {})
        players = []
        for pid in r.get("players") or []:
            v = values.get(str(pid))
            players.append({
                "player": table.get(pid, pid),
                "value": v["value"] if v else 0,
                "overall_rank": v["overall_rank"] if v else None,
            })
        players.sort(key=lambda x: -(x["value"] or 0))
        teams.append({
            "team": (owner.get("metadata") or {}).get("team_name") or owner.get("display_name"),
            "total_value": sum(p["value"] or 0 for p in players),
            "players": players,
        })
    teams.sort(key=lambda t: -t["total_value"])
    return [{"settings_used": {"num_qbs": num_qbs, "ppr": ppr, "num_teams": num_teams, "dynasty": dynasty}}] + teams


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
