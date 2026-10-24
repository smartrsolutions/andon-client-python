"""
The words a refusal uses for the parts of an update. The rules are those of the
TypeScript package, whose API calls them options.at and options.series; an error
has to name what the person in front of it actually wrote, so the runtime takes
the words from its caller.
"""

from __future__ import annotations

from typing import NamedTuple


class Words(NamedTuple):
    topic: str
    """what names the tile"""
    payload: str
    """what carries the value"""
    timestamp: str
    """what carries a sample's or a segment's time"""
    series: str
    """what picks a series by name"""
    lane: str
    """what picks a timeline lane by name"""
    template: str
    """how a template gets to the runtime, for "there is no template yet: ..." """
    times: str
    """what a time may be, for "... is neither <times>" """


API_WORDS = Words(
    topic='the tile ID',
    payload='the value',
    timestamp='the at argument',
    series='the series argument',
    lane='the lane argument',
    template='pass one as the template argument',
    times='a datetime, seconds nor an ISO timestamp',
)
"""The words of this package's own API, AndonView.set(tile_id, value, at=, series=, lane=)."""

# The words of the TypeScript package's API. The parity tests replay what that
# package answered, word for word, so they need its words.
TS_WORDS = Words(
    topic='the tile ID',
    payload='the value',
    timestamp='options.at',
    series='options.series',
    lane='options.lane',
    template='pass one as the template option',
    times='milliseconds nor an ISO timestamp',
)
