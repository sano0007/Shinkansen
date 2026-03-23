"""Shared utilities for anime-pahe-dl."""

from typing import Optional

from anime_pahe_dl.client import Source


def parse_range(range_str: str) -> list[int]:
    """Parse episode range like '1-12' or '1,3,5-7' into list of ints."""
    episodes = []
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            episodes.extend(range(int(start), int(end) + 1))
        else:
            episodes.append(int(part))
    return sorted(set(episodes))


def select_source(
    sources: list[Source],
    quality: str = "best",
    prefer_dub: bool = False,
) -> Optional[Source]:
    """Select the best matching source from available options."""
    if not sources:
        return None

    # Filter by audio preference
    preferred = [s for s in sources if s.is_dub == prefer_dub]
    if not preferred:
        preferred = sources  # Fall back to whatever is available

    if quality == "best":
        # Sort by quality descending, pick highest
        preferred.sort(
            key=lambda s: int(s.quality.replace("p", "") or "0"), reverse=True
        )
        return preferred[0]
    elif quality == "worst":
        preferred.sort(key=lambda s: int(s.quality.replace("p", "") or "0"))
        return preferred[0]
    else:
        # Try exact match first
        target_q = quality if quality.endswith("p") else f"{quality}p"
        exact = [s for s in preferred if s.quality == target_q]
        if exact:
            return exact[0]
        # Fall back to closest
        preferred.sort(
            key=lambda s: abs(
                int(s.quality.replace("p", "") or "0") - int(quality.replace("p", ""))
            )
        )
        return preferred[0]
