"""SRT and WebVTT subtitle parsing, structural validation, serialization, and normalization.

Supports Unicode (Turkish diacritics, Arabic RTL text, Cyrillic, etc.) and
performs strict deterministic validation before any subtitle track is accepted
or uploaded.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.services.validation.validators import ValidationIssue, ValidationReport


class SubtitleFormat(StrEnum):
    SRT = "srt"
    VTT = "vtt"


# SRT timestamp: HH:MM:SS,mmm (minutes and seconds 00-59, milliseconds 000-999)
_SRT_TIME_RE = re.compile(r"^(?P<h>\d{2,}):(?P<m>[0-5]\d):(?P<s>[0-5]\d),(?P<ms>\d{3})$")
# WebVTT timestamp: [HH:]MM:SS.mmm
_VTT_TIME_RE = re.compile(r"^(?:(?P<h>\d{2,}):)?(?P<m>[0-5]\d):(?P<s>[0-5]\d)\.(?P<ms>\d{3})$")

# Pattern matching an SRT cue header (index line + timing line)
_SRT_CUE_HEADER_RE = re.compile(
    r"(?m)^(?P<idx>\d+)[ \t]*\n"
    r"(?P<timing>\d{2,}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2,}:\d{2}:\d{2},\d{3}[^\n]*)\n?"
)


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    """Normalized subtitle cue with millisecond timestamps."""

    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class ParsedSubtitleDocument:
    """Result of parsing and validating an SRT or WebVTT document."""

    format: SubtitleFormat
    cues: tuple[SubtitleCue, ...]
    content_hash: str
    report: ValidationReport

    @property
    def is_valid(self) -> bool:
        return self.report.passed


def format_srt_timestamp(ms: int) -> str:
    """Format milliseconds into ``HH:MM:SS,mmm`` SRT timestamp."""
    if ms < 0:
        raise ValueError(f"Timestamp milliseconds cannot be negative: {ms}")
    total_seconds, millis = divmod(ms, 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_vtt_timestamp(ms: int) -> str:
    """Format milliseconds into ``HH:MM:SS.mmm`` WebVTT timestamp."""
    if ms < 0:
        raise ValueError(f"Timestamp milliseconds cannot be negative: {ms}")
    total_seconds, millis = divmod(ms, 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def serialize_subtitles_to_srt(cues: Sequence[SubtitleCue]) -> str:
    """Serialize normalized subtitle cues into canonical UTF-8 SRT text."""
    blocks: list[str] = []
    for seq_idx, cue in enumerate(cues, start=1):
        start_str = format_srt_timestamp(cue.start_ms)
        end_str = format_srt_timestamp(cue.end_ms)
        clean_lines = [line.strip() for line in cue.text.splitlines() if line.strip()]
        body = "\n".join(clean_lines)
        blocks.append(f"{seq_idx}\n{start_str} --> {end_str}\n{body}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def serialize_subtitles_to_vtt(cues: Sequence[SubtitleCue]) -> str:
    """Serialize normalized subtitle cues into canonical UTF-8 WebVTT text."""
    blocks: list[str] = ["WEBVTT"]
    for seq_idx, cue in enumerate(cues, start=1):
        start_str = format_vtt_timestamp(cue.start_ms)
        end_str = format_vtt_timestamp(cue.end_ms)
        clean_lines = [line.strip() for line in cue.text.splitlines() if line.strip()]
        body = "\n".join(clean_lines)
        blocks.append(f"{seq_idx}\n{start_str} --> {end_str}\n{body}")
    return "\n\n".join(blocks) + "\n"


def _parse_timestamp_ms(raw: str, fmt: SubtitleFormat) -> int | None:
    pattern = _SRT_TIME_RE if fmt is SubtitleFormat.SRT else _VTT_TIME_RE
    match = pattern.match(raw.strip())
    if not match:
        return None
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis = int(match.group("ms"))
    return ((hours * 3600 + minutes * 60 + seconds) * 1000) + millis


def _parse_srt_timing_line(timing_line: str) -> tuple[int, int] | None:
    """Parse an SRT ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` line into ``(start_ms, end_ms)``."""
    if "-->" not in timing_line:
        return None
    left_part, _, right_part = timing_line.partition("-->")
    start_token = left_part.strip()
    end_token = right_part.strip().split()[0] if right_part.strip() else ""
    start_ms = _parse_timestamp_ms(start_token, SubtitleFormat.SRT)
    end_ms = _parse_timestamp_ms(end_token, SubtitleFormat.SRT)
    if start_ms is None or end_ms is None:
        return None
    return start_ms, end_ms


def _resolve_srt_cue_timings(timings: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Repair overlapping, non-chronological, or zero-duration SRT cue timestamps.

    YouTube ASR (rolling 2-line captions) and creator-edited SRT exports routinely emit
    cues where ``cue[i].end_ms > cue[i+1].start_ms`` because line 1 remains visible while
    line 2 rolls in. Clamping ``cue[i].end_ms`` to ``cue[i+1].start_ms`` converts rolling
    display durations into standard non-overlapping SRT cues without altering cue start times.
    """
    if not timings:
        return []

    starts: list[int] = []
    ends: list[int] = []
    for raw_start, raw_end in timings:
        start_ms = max(0, raw_start)
        if starts and start_ms <= starts[-1]:
            start_ms = starts[-1] + 10
        starts.append(start_ms)
        ends.append(raw_end)

    resolved: list[tuple[int, int]] = []
    total = len(starts)
    for i in range(total):
        start_ms = starts[i]
        end_ms = ends[i]
        if i + 1 < total:
            next_start = starts[i + 1]
            if end_ms > next_start:
                end_ms = next_start
            if end_ms <= start_ms:
                end_ms = min(start_ms + 500, next_start)
        else:
            if end_ms <= start_ms:
                end_ms = start_ms + 500
        resolved.append((start_ms, end_ms))

    return resolved


def normalize_youtube_tlang_srt(raw_content: str | bytes) -> tuple[str, list[int]]:
    """Repair internal blank lines and overlapping timestamps in YouTube ``tlang`` SRT cues.

    Live Stage 3b testing showed that ``captions.download?tfmt=srt&tlang=en`` can
    emit an empty first line inside a two-line cue (causing ``\\n\\n`` between the
    timing line and the second text line), leave a cue body completely empty when
    Google Translate merges adjacent short cues, or carry over ASR rolling timestamp
    overlaps.

    Returns:
        ``(normalized_srt_text, empty_cue_indices)``
    """
    decoded, _ = _decode_subtitle_bytes(raw_content)
    if not decoded:
        return "", []
    text = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    matches = list(_SRT_CUE_HEADER_RE.finditer(text))
    if not matches:
        return text, []

    parsed_entries: list[tuple[int, str, tuple[int, int] | None, list[str]]] = []
    empty_indices: list[int] = []

    for i, match in enumerate(matches):
        idx = int(match.group("idx"))
        timing = match.group("timing").strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_body = text[body_start:body_end]
        non_empty_lines = [ln.strip() for ln in raw_body.split("\n") if ln.strip()]
        if not non_empty_lines:
            empty_indices.append(idx)
        parsed_entries.append((idx, timing, _parse_srt_timing_line(timing), non_empty_lines))

    if all(entry[2] is not None for entry in parsed_entries):
        resolved_timings = _resolve_srt_cue_timings(
            [entry[2] for entry in parsed_entries if entry[2] is not None]
        )
    else:
        resolved_timings = []

    rebuilt_blocks: list[str] = []
    for i, (idx, raw_timing, _, non_empty_lines) in enumerate(parsed_entries):
        if resolved_timings:
            s_ms, e_ms = resolved_timings[i]
            timing_str = f"{format_srt_timestamp(s_ms)} --> {format_srt_timestamp(e_ms)}"
        else:
            timing_str = raw_timing
        if not non_empty_lines:
            rebuilt_blocks.append(f"{idx}\n{timing_str}\n")
        else:
            body_str = "\n".join(non_empty_lines)
            rebuilt_blocks.append(f"{idx}\n{timing_str}\n{body_str}")

    return "\n\n".join(rebuilt_blocks) + "\n", empty_indices


def normalize_source_srt(raw_content: str | bytes) -> str:
    """Repair YouTube Studio & ASR source SRT quirks (blank lines, empty blocks, overlapping cues).

    YouTube's ``captions.download?tfmt=srt`` for source tracks can include:
    1. Internal blank lines inside a multi-line cue (which naively splits one cue into an
       ``empty_cue_text`` block plus a ``malformed_cue_block`` without a timing header).
    2. Blank timing slots where caption text was cleared in YouTube Studio without deleting
       the cue slot (``empty_cue_text``).
    3. Overlapping cue timestamps (``cue[i].end_ms > cue[i+1].start_ms``) on ``asr`` rolling
       caption tracks or manually edited captions.

    This normalizer splits by cue headers (``idx + timing``), collapses internal blank lines,
    drops empty source cues, clamps overlapping cue durations, and re-indexes sequentially ``1..N``.
    """
    decoded, _ = _decode_subtitle_bytes(raw_content)
    if not decoded:
        return ""
    text = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    matches = list(_SRT_CUE_HEADER_RE.finditer(text))
    if not matches:
        return text

    kept_entries: list[tuple[str, tuple[int, int] | None, str]] = []
    for i, match in enumerate(matches):
        timing = match.group("timing").strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_body = text[body_start:body_end]
        non_empty_lines = [ln.strip() for ln in raw_body.split("\n") if ln.strip()]
        if not non_empty_lines:
            continue
        body_str = "\n".join(non_empty_lines)
        kept_entries.append((timing, _parse_srt_timing_line(timing), body_str))

    if not kept_entries:
        return ""

    if all(entry[1] is not None for entry in kept_entries):
        resolved_timings = _resolve_srt_cue_timings(
            [entry[1] for entry in kept_entries if entry[1] is not None]
        )
    else:
        resolved_timings = []

    rebuilt_blocks: list[str] = []
    for i, (raw_timing, _, body_str) in enumerate(kept_entries):
        seq_idx = i + 1
        if resolved_timings:
            s_ms, e_ms = resolved_timings[i]
            timing_str = f"{format_srt_timestamp(s_ms)} --> {format_srt_timestamp(e_ms)}"
        else:
            timing_str = raw_timing
        rebuilt_blocks.append(f"{seq_idx}\n{timing_str}\n{body_str}")

    return "\n\n".join(rebuilt_blocks) + "\n"


def _decode_subtitle_bytes(content: str | bytes) -> tuple[str | None, ValidationIssue | None]:
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return None, ValidationIssue(
                code="invalid_utf8_encoding",
                message=f"Subtitle file is not valid UTF-8: {exc}",
            )
    else:
        text = content.lstrip("\ufeff")

    if "\ufffd" in text:
        return text, ValidationIssue(
            code="unicode_replacement_character",
            message="Subtitle text contains Unicode replacement character (U+FFFD / mojibake).",
        )
    return text, None


def parse_and_validate_subtitles(
    content: str | bytes,
    fmt: SubtitleFormat,
    *,
    reference_cues: tuple[SubtitleCue, ...] | None = None,
    max_timing_drift_ms: int = 1000,
    max_line_length: int | None = None,
) -> ParsedSubtitleDocument:
    """Parse an SRT or WebVTT document and run deterministic validation checks.

    Checks:
    - Valid UTF-8 without U+FFFD replacement characters
    - Non-empty content & WebVTT `WEBVTT` header when `fmt == SubtitleFormat.VTT`
    - Valid cue numbering (for SRT) and `start --> end` timing lines
    - Valid timestamp syntax (`HH:MM:SS,mmm` for SRT; `[HH:]MM:SS.mmm` for VTT)
    - Positive cue duration (`end_ms > start_ms`)
    - Chronological ordering (`cue[i].start_ms >= cue[i-1].start_ms` and no overlaps)
    - Non-empty cue text (no blank/missing segments)
    - Optional `max_line_length` enforcement (`cue_line_too_long`)
    - Optional parity check against `reference_cues` (cue count and timestamp alignment)
    """
    issues: list[ValidationIssue] = []
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"

    decoded, encoding_issue = _decode_subtitle_bytes(content)
    if encoding_issue is not None:
        issues.append(encoding_issue)
    if decoded is None:
        return ParsedSubtitleDocument(
            format=fmt,
            cues=(),
            content_hash=digest,
            report=ValidationReport(passed=False, issues=tuple(issues)),
        )

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        issues.append(
            ValidationIssue(
                code="empty_subtitle_content",
                message="Subtitle file is empty.",
            )
        )
        return ParsedSubtitleDocument(
            format=fmt,
            cues=(),
            content_hash=digest,
            report=ValidationReport(passed=False, issues=tuple(issues)),
        )

    blocks = re.split(r"\n\s*\n", normalized)
    if fmt is SubtitleFormat.VTT:
        first_block_lines = blocks[0].split("\n")
        header_line = first_block_lines[0].strip()
        if not (header_line == "WEBVTT" or header_line.startswith("WEBVTT ")):
            issues.append(
                ValidationIssue(
                    code="missing_webvtt_header",
                    message="WebVTT file must begin with a 'WEBVTT' header.",
                )
            )
            return ParsedSubtitleDocument(
                format=fmt,
                cues=(),
                content_hash=digest,
                report=ValidationReport(passed=False, issues=tuple(issues)),
            )
        blocks = blocks[1:]

    cues: list[SubtitleCue] = []
    expected_srt_index = 1

    for block_pos, raw_block in enumerate(blocks, start=1):
        lines = [line.strip() for line in raw_block.split("\n")]
        if not any(lines):
            continue

        # In WebVTT, skip NOTE or STYLE metadata blocks
        if fmt is SubtitleFormat.VTT and lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue

        timing_line_idx = 0
        cue_index = len(cues) + 1

        if fmt is SubtitleFormat.SRT:
            if len(lines) < 2:
                issues.append(
                    ValidationIssue(
                        code="malformed_cue_block",
                        message=f"SRT block {block_pos} is missing timing or text lines.",
                    )
                )
                continue
            if not lines[0].isdigit():
                issues.append(
                    ValidationIssue(
                        code="invalid_cue_index",
                        message=f"SRT block {block_pos} has non-numeric cue index {lines[0]!r}.",
                    )
                )
                continue
            cue_index = int(lines[0])
            if cue_index != expected_srt_index:
                issues.append(
                    ValidationIssue(
                        code="non_sequential_cue_index",
                        message=(
                            f"SRT cue index {cue_index} is out of sequence "
                            f"(expected {expected_srt_index})."
                        ),
                    )
                )
            expected_srt_index = cue_index + 1
            timing_line_idx = 1
        else:
            # WebVTT optional cue identifier before the timing line
            if "-->" not in lines[0]:
                if len(lines) < 2 or "-->" not in lines[1]:
                    issues.append(
                        ValidationIssue(
                            code="malformed_cue_block",
                            message=f"WebVTT block {block_pos} is missing a '-->' timing line.",
                        )
                    )
                    continue
                timing_line_idx = 1

        timing_line = lines[timing_line_idx]
        if "-->" not in timing_line:
            issues.append(
                ValidationIssue(
                    code="missing_timestamp_arrow",
                    message=f"Cue {cue_index} timing line {timing_line!r} is missing '-->'.",
                )
            )
            continue

        left_part, _, right_part = timing_line.partition("-->")
        start_token = left_part.strip()
        # WebVTT allows cue settings after end timestamp separated by space
        end_token = right_part.strip().split()[0] if right_part.strip() else ""

        start_ms = _parse_timestamp_ms(start_token, fmt)
        end_ms = _parse_timestamp_ms(end_token, fmt)
        if start_ms is None or end_ms is None:
            issues.append(
                ValidationIssue(
                    code="invalid_timestamp_format",
                    message=(
                        f"Cue {cue_index} has invalid {fmt.value.upper()} timestamp(s): "
                        f"{timing_line!r}."
                    ),
                )
            )
            continue

        if end_ms <= start_ms:
            issues.append(
                ValidationIssue(
                    code="non_positive_cue_duration",
                    message=(
                        f"Cue {cue_index} end timestamp ({end_token}) must be strictly "
                        f"greater than start timestamp ({start_token})."
                    ),
                )
            )

        if cues:
            prev = cues[-1]
            if start_ms < prev.start_ms:
                issues.append(
                    ValidationIssue(
                        code="non_chronological_timestamps",
                        message=(
                            f"Cue {cue_index} starts at {start_ms}ms before previous cue "
                            f"{prev.index} ({prev.start_ms}ms)."
                        ),
                    )
                )
            elif start_ms < prev.end_ms:
                issues.append(
                    ValidationIssue(
                        code="overlapping_cue_timestamps",
                        message=(
                            f"Cue {cue_index} starts at {start_ms}ms before previous cue "
                            f"{prev.index} ends ({prev.end_ms}ms)."
                        ),
                    )
                )

        text_lines = [line for line in lines[timing_line_idx + 1 :] if line != ""]
        cue_text = "\n".join(text_lines).strip()
        if not cue_text:
            issues.append(
                ValidationIssue(
                    code="empty_cue_text",
                    message=f"Cue {cue_index} has empty subtitle text (missing segment).",
                )
            )
        elif max_line_length is not None:
            for line_num, text_line in enumerate(text_lines, start=1):
                if len(text_line) > max_line_length:
                    issues.append(
                        ValidationIssue(
                            code="cue_line_too_long",
                            message=(
                                f"Cue {cue_index} line {line_num} is {len(text_line)} chars "
                                f"(max {max_line_length})."
                            ),
                        )
                    )

        cues.append(
            SubtitleCue(
                index=cue_index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=cue_text,
            )
        )

    if not cues and not issues:
        issues.append(
            ValidationIssue(
                code="no_cues_found",
                message="Subtitle file contains no subtitle cues.",
            )
        )

    if reference_cues is not None and cues:
        if len(cues) != len(reference_cues):
            issues.append(
                ValidationIssue(
                    code="missing_or_extra_segments",
                    message=(
                        f"Translated subtitle has {len(cues)} cues, but source track "
                        f"has {len(reference_cues)} cues."
                    ),
                )
            )
        else:
            for src_cue, tgt_cue in zip(reference_cues, cues, strict=True):
                if (
                    abs(src_cue.start_ms - tgt_cue.start_ms) > max_timing_drift_ms
                    or abs(src_cue.end_ms - tgt_cue.end_ms) > max_timing_drift_ms
                ):
                    issues.append(
                        ValidationIssue(
                            code="timestamp_drift_from_source",
                            message=(
                                f"Cue {tgt_cue.index} timestamps "
                                f"({tgt_cue.start_ms}..{tgt_cue.end_ms}ms) "
                                f"drift from source cue ({src_cue.start_ms}..{src_cue.end_ms}ms)."
                            ),
                        )
                    )

    report = ValidationReport(
        passed=not any(i.blocking for i in issues),
        issues=tuple(issues),
    )
    return ParsedSubtitleDocument(
        format=fmt,
        cues=tuple(cues),
        content_hash=digest,
        report=report,
    )
