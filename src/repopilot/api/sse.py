"""Serialize persisted events for cursor-based EventSource replay."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from repopilot.domain.models import TaskEvent


def event_stream(events: Iterable[TaskEvent]) -> Iterator[str]:
    last_sequence = 0
    for event in events:
        last_sequence = event.sequence
        payload = json.dumps(
            event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        )
        yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
    yield (
        f"id: {last_sequence}\n"
        "event: stream.end\n"
        f'data: {{"last_sequence":{last_sequence}}}\n\n'
    )

