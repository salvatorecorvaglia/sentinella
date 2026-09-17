from rich.text import Text

from sentinella.core.utils import (
    color_for_percent,
    human_bytes,
    human_bytes_compact,
    render_bar,
    text_sparkline,
)


def test_human_bytes():
    assert human_bytes(0) == "0.0 B"
    assert human_bytes(1023) == "1023.0 B"
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(1024 * 1024 * 1.5) == "1.5 MB"
    assert human_bytes(1024 * 1024 * 1024 * 3) == "3.0 GB"
    assert human_bytes(-500) == "-500.0 B"


def test_human_bytes_compact():
    assert human_bytes_compact(0) == "0.0B"
    assert human_bytes_compact(512) == "512B"
    assert human_bytes_compact(1024) == "1KB"
    assert human_bytes_compact(1024 * 1024 * 2.5) == "2MB"
    # Let's verify our human_bytes_compact implementation format
    # It says: return f"{n:.0f}{unit}" if n >= 1 else f"{n:.1f}{unit}"
    # So for 1024 * 1024 * 2.5: n = 2.5, returns "3MB" or "2MB" depending
    # on the rounding mode.
    # Let's check 1024: n = 1, returns "1KB"
    # Let's check 512: n = 512, returns "512B"


def test_color_for_percent():
    assert color_for_percent(10.0) == "#00d2ff"
    assert color_for_percent(55.0) == "#f59e0b"
    assert color_for_percent(85.0) == "#ef4444"
    assert color_for_percent(50.0, thresholds=(60.0, 90.0)) == "#00d2ff"


def test_text_sparkline():
    assert text_sparkline([]) == ""
    assert text_sparkline([10.0]) == ""
    # Check block character scaling
    spark = text_sparkline([0, 50, 100])
    assert len(spark) == 3
    assert spark[0] == " "
    assert spark[2] == "█"


def test_render_bar():
    # Standard bar
    bar = render_bar(50.0, width=10)
    assert isinstance(bar, Text)
    # 5 filled, 5 empty
    assert bar.plain.startswith("━━━━━─────")

    # Bracketed bar
    bracketed_bar = render_bar(50.0, width=10, bracketed=True)
    assert bracketed_bar.plain.startswith("[━━━━━─────]")
