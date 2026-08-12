"""Send a CSV export as it is read, instead of building it in memory first.

An export used to be materialized twice: once as a list of every matching row,
and again as one string holding the finished file. Both grow with the result
set, so a fleet-wide export is bounded only by how much the database will
return — the process that answers a routine request is the one that runs out of
memory. Streaming keeps a single row in flight instead.

The row iterator must stay lazy for this to be worth anything: pair it with
``yield_per`` so the database side is a cursor rather than a fetched list.
"""

from collections.abc import Iterable, Iterator, Sequence
import csv
from io import StringIO

from fastapi.responses import StreamingResponse

#: Rows fetched per round trip when the caller uses ``yield_per``. Large enough
#: that a big export is not a per-row round trip, small enough that the buffer
#: stays a rounding error next to the response.
CSV_FETCH_BATCH = 1_000


def csv_chunks(
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
) -> Iterator[str]:
    """Yield the header, then one chunk per row, reusing a single buffer.

    ``csv.writer`` is used rather than joining with commas so quoting, embedded
    newlines and the CRLF line ending stay exactly as they were when the whole
    file was written at once.
    """
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)

    def flush() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    writer.writerow(header)
    yield flush()
    for row in rows:
        writer.writerow(row)
        yield flush()


def csv_response(
    header: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    filename: str,
) -> StreamingResponse:
    """A streamed CSV attachment.

    No Content-Length: the size is not known until the last row is read, and
    guessing one would truncate the download.
    """
    return StreamingResponse(
        csv_chunks(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
