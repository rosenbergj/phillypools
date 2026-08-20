"""How long a pool's season was, and the shape of those lengths across the city.

One definition of "days open" lives here — `(closing - opening).days + 1`,
counting both endpoints — because two different parts of the offseason build
quote it: each archived pool page says "7 weeks, 3 days", and the index page
draws a histogram of every pool's length. If those disagreed, the site would be
contradicting itself on a page a reader can see from the page they just left.
"""

from dataclasses import dataclass


def season_length_days(opening_date, closing_date):
    """Days open, counting opening day and closing day, or None if unknowable.

    Returns None rather than 0 for a pool missing either date: "we don't know"
    and "was open no days" are different claims, and only one of them is one we
    can make. Callers decide how to present the difference.
    """
    if not (opening_date and closing_date):
        return None
    total = (closing_date - opening_date).days + 1
    return total if total > 0 else None


def format_duration(opening_date, closing_date):
    """'7 weeks, 3 days' for a completed season, or None if it can't be computed."""
    total = season_length_days(opening_date, closing_date)
    if total is None:
        return None
    weeks, days = divmod(total, 7)
    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


def season_snapshot(pool, season_year):
    """Everything the static page needs about `pool`'s `season_year`.

    Reads PoolSeasonHistory first, since that's the durable record, but falls
    back to the pool's live fields. The fallback is load-bearing in two cases:

      * `_upsert_season_history` only creates a history row when the pool has an
        opening or closing date (models.py), so a pool that got schedule text but
        never got dates has no row at all — without the fallback its page would
        render with no hours.
      * `render_static_site` runs before `reset_season` (which stays at the
        *start* of the next season), so the live fields are still populated and
        are the freshest copy of the schedule.
    """
    history = pool.season_history.filter(year=season_year).first()

    opening_date = history.opening_date if history else None
    closing_date = history.closing_date if history else None
    weekday_schedule = (history.weekday_schedule if history else "") or ""
    weekend_schedule = (history.weekend_schedule if history else "") or ""

    # Only trust the live dates if they belong to the season being rendered; a
    # stale prior-season date would otherwise be relabeled as this season's.
    if not opening_date and pool.opening_date and pool.opening_date.year == season_year:
        opening_date = pool.opening_date
    if not closing_date and pool.closing_date and pool.closing_date.year == season_year:
        closing_date = pool.closing_date

    if not weekday_schedule:
        weekday_schedule = pool.weekday_schedule or ""
    if not weekend_schedule:
        weekend_schedule = pool.weekend_schedule or ""

    return {
        "opening_date": opening_date,
        "closing_date": closing_date,
        "weekday_schedule": weekday_schedule,
        "weekend_schedule": weekend_schedule,
        "duration": format_duration(opening_date, closing_date),
    }


# ---------------------------------------------------------------------------
# Histogram of season lengths, for the offseason index page.
#
# Geometry is computed here rather than in the template because binning and
# axis-tick placement are arithmetic, and Django's template language is a bad
# place to do arithmetic. The template's job is styling: it receives numbers
# already in viewBox units and emits <rect>s.

VIEW_WIDTH = 720
VIEW_HEIGHT = 320
PAD_LEFT = 38
PAD_RIGHT = 14
PAD_TOP = 14
# Deep enough to stack the day-number ticks and the "days open" title without
# them touching once the narrow-screen rule enlarges the type (see the template).
PAD_BOTTOM = 62

DEFAULT_BIN_WIDTH = 2


@dataclass(frozen=True)
class HistogramBar:
    start: int          # first day-count falling in this bin
    end: int            # last day-count falling in this bin, inclusive
    count: int
    label: str          # "56–57", or "57" when bins are one day wide
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class AxisTick:
    value: int
    position: float     # viewBox coordinate along the relevant axis
    label: str


@dataclass(frozen=True)
class SeasonHistogram:
    bars: list
    x_ticks: list
    y_ticks: list
    bin_width: int
    counted: int        # pools with both dates, i.e. the bars' total
    uncounted: int      # pools we couldn't measure, named in the caption
    shortest: int
    longest: int
    median: float
    view_width: int = VIEW_WIDTH
    view_height: int = VIEW_HEIGHT
    plot_left: float = PAD_LEFT
    plot_right: float = VIEW_WIDTH - PAD_RIGHT
    plot_top: float = PAD_TOP
    plot_bottom: float = VIEW_HEIGHT - PAD_BOTTOM

    @property
    def plot_middle(self):
        """Vertical centre of the plot area, for the rotated y-axis label."""
        return round((self.plot_top + self.plot_bottom) / 2, 2)

    @property
    def median_label(self):
        """'57' rather than '57.0'; '57.5' stays as it is."""
        return f"{self.median:g}"

    @property
    def summary(self):
        """One sentence, used as the SVG's accessible description.

        A screen reader gets the shape of the data in words; the bars themselves
        are decorative once this has been read.
        """
        return (
            f"Histogram of how many days each Philadelphia public pool was open. "
            f"{self.counted} pool{'s' if self.counted != 1 else ''}, grouped into "
            f"{self.bin_width}-day bucket{'s' if self.bin_width != 1 else ''}. "
            f"The shortest season was {self.shortest} days, the longest "
            f"{self.longest}, and the median {self.median_label}."
        )


def _nice_y_step(max_count):
    """A tick interval that gives a readable number of gridlines at any scale."""
    for limit, step in ((5, 1), (12, 2), (30, 5), (60, 10)):
        if max_count <= limit:
            return step
    return 20


def build_season_histogram(lengths, bin_width=DEFAULT_BIN_WIDTH, uncounted=0):
    """Bin `lengths` (a list of day counts) and lay the bars out in viewBox units.

    Bins are anchored to multiples of `bin_width` rather than to the shortest
    season, so that re-rendering after a single pool's date changes shifts one
    bar rather than re-cutting every bucket boundary.

    Returns None for an empty list — the caller omits the chart entirely rather
    than publishing an empty pair of axes.
    """
    if not lengths:
        return None
    if bin_width < 1:
        raise ValueError(f"bin_width must be at least 1 day, got {bin_width}")

    lengths = sorted(lengths)
    lo = (lengths[0] // bin_width) * bin_width
    hi = (lengths[-1] // bin_width) * bin_width  # start of the last occupied bin

    counts = {}
    for value in lengths:
        counts[(value // bin_width) * bin_width] = counts.get((value // bin_width) * bin_width, 0) + 1

    starts = list(range(lo, hi + 1, bin_width))
    span = (hi + bin_width) - lo  # in days, so day-space maps linearly to x
    plot_w = (VIEW_WIDTH - PAD_RIGHT) - PAD_LEFT
    plot_h = (VIEW_HEIGHT - PAD_BOTTOM) - PAD_TOP
    baseline = VIEW_HEIGHT - PAD_BOTTOM

    def x_of(day_value):
        return PAD_LEFT + (day_value - lo) / span * plot_w

    slot = bin_width / span * plot_w
    gap = min(2.0, slot * 0.18)

    y_step = _nice_y_step(max(counts.values()))
    y_top = -(-max(counts.values()) // y_step) * y_step  # ceil to a whole tick

    bars = []
    for start in starts:
        count = counts.get(start, 0)
        height = count / y_top * plot_h
        end = start + bin_width - 1
        bars.append(
            HistogramBar(
                start=start,
                end=end,
                count=count,
                label=str(start) if bin_width == 1 else f"{start}–{end}",
                x=round(x_of(start) + gap / 2, 2),
                y=round(baseline - height, 2),
                width=round(slot - gap, 2),
                height=round(height, 2),
            )
        )

    # Label the day axis on round tens, which read as days rather than as bucket
    # boundaries — the buckets are already drawn, and labelling all 26 of them
    # would be unreadable at any width the page actually gets.
    x_ticks = [
        AxisTick(value=v, position=round(x_of(v), 2), label=str(v))
        for v in range(-(-lo // 10) * 10, hi + bin_width + 1, 10)
    ]
    y_ticks = [
        AxisTick(
            value=v,
            position=round(baseline - v / y_top * plot_h, 2),
            label=str(v),
        )
        for v in range(0, y_top + 1, y_step)
    ]

    middle = len(lengths) // 2
    median = (
        lengths[middle]
        if len(lengths) % 2
        else (lengths[middle - 1] + lengths[middle]) / 2
    )

    return SeasonHistogram(
        bars=bars,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
        bin_width=bin_width,
        counted=len(lengths),
        uncounted=uncounted,
        shortest=lengths[0],
        longest=lengths[-1],
        median=median,
    )
