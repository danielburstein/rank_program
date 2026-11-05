"""Run an extractor and parse its rows into structured detections."""

from __future__ import annotations

from dataclasses import dataclass, field

from rankrecovery.core.parsing import ParsedDetection, RowParser
from rankrecovery.extraction.base import DetectedRegion, ExtractionOutput, TextExtractor


@dataclass(slots=True)
class PipelineResult:
    output: ExtractionOutput
    detections: list[ParsedDetection] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output.ok


def parse_output(output: ExtractionOutput, parser: RowParser) -> list[ParsedDetection]:
    detections: list[ParsedDetection] = []
    for row in output.rows:
        try:
            parsed = parser.parse(row)
        except Exception as exc:  # a single bad row must not kill the image
            output.warnings.append(f"row parse failed: {exc}")
            continue
        if parsed is not None:
            detections.append(parsed)
    return detections


def run_extraction(
    image_bytes: bytes,
    extractor: TextExtractor,
    parser: RowParser,
    *,
    filename: str = "",
    regions: list[DetectedRegion] | None = None,
) -> PipelineResult:
    output = extractor.extract(image_bytes, filename=filename, regions=regions)
    detections = parse_output(output, parser) if output.rows else []
    return PipelineResult(output=output, detections=detections)
