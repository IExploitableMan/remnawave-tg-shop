"""Read-only stable release discovery within the approved node release line."""
import re
import time
import aiohttp

_cache = {}


def version_tuple(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value or '')
    return tuple(map(int, match.groups())) if match else None


async def latest_node_version(approved):
    line = version_tuple(approved)
    if not line:
        return None
    line = line[:2]
    cached = _cache.get(line)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
            async with client.get('https://api.github.com/repos/remnawave/node/releases?per_page=100',
                                  headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'remnawave-tg-shop'}) as response:
                response.raise_for_status()
                releases = await response.json()
        candidates = [version_tuple(r.get('tag_name')) for r in releases
                      if not r.get('draft') and not r.get('prerelease')]
        candidates = [v for v in candidates if v and v[:2] == line]
        latest = '.'.join(map(str, max(candidates))) if candidates else None
        _cache[line] = time.monotonic(), latest
        return latest
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        return None
