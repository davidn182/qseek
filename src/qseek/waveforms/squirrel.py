from __future__ import annotations

import asyncio
import glob
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Literal

from pydantic import (
    AwareDatetime,
    DirectoryPath,
    Field,
    PositiveInt,
    PrivateAttr,
    computed_field,
    field_validator,
    model_validator,
)
from pyrocko.squirrel import Squirrel
from typing_extensions import Self

from qseek.models.station import Stations
from qseek.stats import Stats
from qseek.utils import datetime_now, human_readable_bytes, to_datetime
from qseek.waveforms.base import WaveformBatch, WaveformProvider

if TYPE_CHECKING:
    from pyrocko.squirrel.base import Batch
    from rich.table import Table

logger = logging.getLogger(__name__)


class SquirrelPrefetcher:
    queue: asyncio.Queue[Batch | None]
    load_time: timedelta = timedelta(seconds=0.0)

    _load_queue: asyncio.Queue[Batch | None]
    _fetched_batches: int
    _task: asyncio.Task[None]

    def __init__(
        self,
        iterator: AsyncIterator[Batch],
        queue_size: int = 8,
    ) -> None:
        self.iterator = iterator
        self.queue = asyncio.Queue(maxsize=queue_size)
        self._load_queue = asyncio.Queue(maxsize=queue_size)
        self._fetched_batches = 0
        self._task = asyncio.create_task(self.prefetch_worker())

    async def prefetch_worker(self) -> None:
        logger.info(
            "start pre-fetching data, queue size %d",
            self.queue.maxsize,
        )

        start = datetime_now()
        async for batch in self.iterator:
            self.load_time = datetime_now() - start
            self._fetched_batches += 1
            logger.debug("read waveform batch in %s", self.load_time)
            start = datetime_now()
            await self.queue.put(batch)

        await self.queue.put(None)


class SquirrelStats(Stats):
    empty_batches: PositiveInt = 0
    short_batches: PositiveInt = 0
    time_per_batch: timedelta = timedelta(seconds=0.0)
    bytes_per_seconds: float = 0.0

    _queue: asyncio.Queue[Batch | None] | None = PrivateAttr(None)

    def set_queue(self, queue: asyncio.Queue[Batch | None]) -> None:
        self._queue = queue

    @computed_field
    @property
    def queue_size(self) -> PositiveInt:
        if self._queue is None:
            return 0
        return self._queue.qsize()

    @computed_field
    @property
    def queue_size_max(self) -> PositiveInt:
        if self._queue is None:
            return 0
        return self._queue.maxsize

    def _populate_table(self, table: Table) -> None:
        prefix = "[bold red]" if self.queue_size <= 1 else ""
        table.add_row("Queue", f"{prefix}{self.queue_size} / {self.queue_size_max}")
        table.add_row(
            "Waveform loading",
            f"{human_readable_bytes(self.bytes_per_seconds)}/s",
        )


class PyrockoSquirrel(WaveformProvider):
    """Waveform provider using Pyrocko's Squirrel."""

    provider: Literal["PyrockoSquirrel"] = "PyrockoSquirrel"

    environment: DirectoryPath = Field(
        default=Path("."),
        description="Path to a Squirrel environment.",
    )
    waveform_dirs: list[Path] = Field(
        default=[],
        description="List of directories holding the waveform files.",
    )
    start_time: AwareDatetime | None = Field(
        default=None,
        description="Start time for the search in "
        "[ISO8601](https://en.wikipedia.org/wiki/ISO_8601).",
    )
    end_time: AwareDatetime | None = Field(
        default=None,
        description="End time for the search in "
        "[ISO8601](https://en.wikipedia.org/wiki/ISO_8601).",
    )

    channel_selector: str = Field(
        default="*",
        max_length=3,
        description="Channel selector for waveforms, "
        "use e.g. `EN?` for selection of all accelerometer data.",
    )
    async_prefetch_batches: PositiveInt = Field(
        default=10,
        description="Queue size for asynchronous pre-fetcher.",
    )

    _squirrel: Squirrel | None = PrivateAttr(None)
    _stations: Stations = PrivateAttr(None)
    _stats: SquirrelStats = PrivateAttr(default_factory=SquirrelStats)

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        if self.start_time and self.end_time and self.start_time > self.end_time:
            raise ValueError("start_time must be before end_time")
        return self

    @field_validator("waveform_dirs")
    def check_dirs(cls, dirs: list[Path]) -> list[Path]:  # noqa: N805
        if not dirs:
            raise ValueError("no waveform directories provided!")
        return dirs

    def get_squirrel(self) -> Squirrel:
        if not self._squirrel:
            logger.info("initializing squirrel waveform provider")
            squirrel = Squirrel(str(self.environment.expanduser()))
            paths = []
            for path in self.waveform_dirs:
                if "**" in str(path):
                    paths.extend(glob.glob(str(path.expanduser()), recursive=True))
                else:
                    paths.append(str(path.expanduser()))

            squirrel.add(paths, check=False)
            if self._stations:
                for path in self._stations.station_xmls:
                    logger.info("loading responses from %s", path)
                    squirrel.add(str(path), check=False)
            self._squirrel = squirrel
        return self._squirrel

    def prepare(self, stations: Stations) -> None:
        logger.info("preparing squirrel waveform provider")
        self._stations = stations
        squirrel = self.get_squirrel()
        stations.weed_from_squirrel_waveforms(squirrel)

    async def iter_batches(
        self,
        window_increment: timedelta,
        window_padding: timedelta,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        min_length: timedelta | None = None,
    ) -> AsyncIterator[WaveformBatch]:
        if not self._stations:
            raise ValueError("no stations provided. has prepare() been called?")

        squirrel = self.get_squirrel()
        stats = self._stats
        sq_tmin, sq_tmax = squirrel.get_time_span(["waveform"])

        start_time = start_time or self.start_time or to_datetime(sq_tmin)
        end_time = end_time or self.end_time or to_datetime(sq_tmax)

        logger.info(
            "searching time span from %s to %s (%s)",
            start_time,
            end_time,
            end_time - start_time,
        )

        iterator = squirrel.chopper_waveforms_async(
            tmin=(start_time + window_padding).timestamp(),
            tmax=(end_time - window_padding).timestamp(),
            tinc=window_increment.total_seconds(),
            tpad=window_padding.total_seconds(),
            want_incomplete=False,
            codes=[
                (*nsl, self.channel_selector) for nsl in self._stations.get_all_nsl()
            ],
        )
        prefetcher = SquirrelPrefetcher(
            iterator, queue_size=self.async_prefetch_batches
        )
        stats.set_queue(prefetcher.queue)

        while True:
            start = datetime_now()
            pyrocko_batch = await prefetcher.queue.get()
            if pyrocko_batch is None:
                prefetcher.queue.task_done()
                break

            batch = WaveformBatch(
                traces=pyrocko_batch.traces,
                start_time=to_datetime(pyrocko_batch.tmin),
                end_time=to_datetime(pyrocko_batch.tmax),
                i_batch=pyrocko_batch.i,
                n_batches=pyrocko_batch.n,
            )

            batch.clean_traces()

            stats.time_per_batch = datetime_now() - start
            stats.bytes_per_seconds = (
                batch.cumulative_bytes / prefetcher.load_time.total_seconds()
            )

            if batch.is_empty():
                logger.warning("empty batch %d", batch.i_batch)
                stats.empty_batches += 1
                continue

            if min_length and batch.duration < min_length:
                logger.warning(
                    "duration of batch %d too short %s",
                    batch.i_batch,
                    batch.duration,
                )
                stats.short_batches += 1
                continue

            yield batch

            prefetcher.queue.task_done()
