"""Create Simmer SDK clients outside the skill root.

SimmerClient auto-detects SKILL.md from its immediate caller and verifies the
published entrypoint hash. During local development this repository's
entrypoint is intentionally modified, so construction from the skill root fails
before we can call read-only APIs such as get_fast_markets().
"""

from simmer_sdk import SimmerClient


def create_simmer_client(api_key, venue, live):
    return SimmerClient(api_key=api_key, venue=venue, live=live)
