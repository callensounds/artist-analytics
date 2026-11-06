"""Ego network from stored related_edges. No provider calls."""

from __future__ import annotations

import math

from history import find_artist_by_id

# One indexed read of the artist's edges, plus one indexed read among those neighbors.
NEIGHBOR_CAP = 12
CROSS_CAP = 24
_LABEL_LIMIT = 22


def _label(name: str) -> str:
    text = (name or "").strip()
    if len(text) <= _LABEL_LIMIT:
        return text
    return text[: _LABEL_LIMIT - 1] + "…"


def layout_ego(center: dict, neighbors: list[dict], cross: list[tuple[int, int]]) -> dict:
    """Place the artist in the middle and stored neighbors on a ring.

    Coordinates are pixels in a 640×420 drawing. Lines connect only ids in that set.
    """
    width, height = 640, 420
    cx, cy, radius = width / 2, height / 2, 150
    nodes: list[dict] = [
        {
            "id": int(center["id"]),
            "name": center.get("name") or "",
            "label": _label(str(center.get("name") or "")),
            "x": round(cx, 1),
            "y": round(cy, 1),
            "center": True,
            "anchor": "middle",
            "label_y": round(cy + 22, 1),
        }
    ]
    count = len(neighbors)
    by_id = {int(center["id"]): nodes[0]}
    for index, neighbor in enumerate(neighbors):
        angle = -math.pi / 2 + (2 * math.pi * index / count)
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        if x < cx - 12:
            anchor = "end"
        elif x > cx + 12:
            anchor = "start"
        else:
            anchor = "middle"
        node = {
            "id": int(neighbor["id"]),
            "name": neighbor.get("name") or "",
            "label": _label(str(neighbor.get("name") or "")),
            "x": round(x, 1),
            "y": round(y, 1),
            "center": False,
            "anchor": anchor,
            "label_y": round(y + (16 if y >= cy else -12), 1),
            "rank": neighbor.get("rank"),
            "last_seen_at": neighbor.get("last_seen_at") or "",
            "first_seen_at": neighbor.get("first_seen_at") or "",
        }
        nodes.append(node)
        by_id[node["id"]] = node
    edges = []
    seen: set[tuple[int, int]] = set()
    for left, right in cross:
        pair = tuple(sorted((int(left), int(right))))
        if pair[0] == pair[1] or pair in seen:
            continue
        start = by_id.get(pair[0])
        end = by_id.get(pair[1])
        if start is None or end is None:
            continue
        seen.add(pair)
        edges.append(
            {
                "x1": start["x"],
                "y1": start["y"],
                "x2": end["x"],
                "y2": end["y"],
                "from_id": int(left),
                "to_id": int(right),
            }
        )
    return {
        "width": width,
        "height": height,
        "center_id": int(center["id"]),
        "center_name": center.get("name") or "",
        "nodes": nodes,
        "edges": edges,
        "neighbors": [node for node in nodes if not node["center"]],
    }


async def load_related_graph(db, artist_id: int) -> dict | None:
    """At most two indexed reads. Neighbors without a D1 row are left out."""
    artist = await find_artist_by_id(db, artist_id)
    if not artist:
        return None
    rows = await db.query(
        """
        SELECT to_name, to_spotify_id, to_artist_id, rank, first_seen_at, last_seen_at
        FROM related_edges
        WHERE from_artist_id = ? AND to_artist_id IS NOT NULL
        ORDER BY rank ASC, last_seen_at DESC
        LIMIT ?
        """,
        (int(artist["id"]), NEIGHBOR_CAP),
    )
    neighbors = []
    seen_ids = {int(artist["id"])}
    for row in rows:
        try:
            neighbor_id = int(row["to_artist_id"])
        except (TypeError, ValueError):
            continue
        if neighbor_id < 1 or neighbor_id in seen_ids:
            continue
        seen_ids.add(neighbor_id)
        neighbors.append(
            {
                "id": neighbor_id,
                "name": str(row.get("to_name") or ""),
                "rank": row.get("rank"),
                "first_seen_at": row.get("first_seen_at") or "",
                "last_seen_at": row.get("last_seen_at") or "",
                "to_spotify_id": str(row.get("to_spotify_id") or ""),
            }
        )
    cross = [(int(artist["id"]), item["id"]) for item in neighbors]
    if neighbors:
        marks = ",".join("?" for _ in neighbors)
        member_ids = [int(artist["id"])] + [item["id"] for item in neighbors]
        member_marks = ",".join("?" for _ in member_ids)
        extra = await db.query(
            f"""
            SELECT from_artist_id, to_artist_id
            FROM related_edges
            WHERE from_artist_id IN ({marks})
              AND to_artist_id IN ({member_marks})
            ORDER BY rank ASC, last_seen_at DESC
            LIMIT ?
            """,
            tuple(item["id"] for item in neighbors) + tuple(member_ids) + (CROSS_CAP,),
        )
        for row in extra:
            try:
                cross.append((int(row["from_artist_id"]), int(row["to_artist_id"])))
            except (TypeError, ValueError):
                continue
    return layout_ego(artist, neighbors, cross)
