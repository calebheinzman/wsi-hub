import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


from .utils import atomic_json

class MetricsRecorder:
    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.records: List[Dict[str, Any]] = []

    def record(self,
               step: str,
               entity: str,
               input_count: Optional[int] = None,
               output_count: Optional[int] = None,
               filtered_count: Optional[int] = None,
               extra: Optional[Dict[str, Any]] = None):
        rec: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step": step,
            "entity": entity,
        }
        if input_count is not None:
            rec["input_count"] = int(input_count)
        if output_count is not None:
            rec["output_count"] = int(output_count)
        if filtered_count is None and input_count is not None and output_count is not None:
            rec["filtered_count"] = max(0, int(input_count) - int(output_count))
        elif filtered_count is not None:
            rec["filtered_count"] = int(filtered_count)
        if extra:
            rec.update(extra)
        self.records.append(rec)

    def build_summary(self) -> Dict[str, Any]:
        by_step: Dict[str, Dict[str, Dict[str, int]]] = {}
        totals: Dict[str, Dict[str, int]] = {}

        for rec in self.records:
            step = rec.get("step")
            entity = rec.get("entity")
            if not step or not entity:
                continue

            step_bucket = by_step.setdefault(step, {})
            ent_bucket = step_bucket.setdefault(entity, {"input": 0, "output": 0, "filtered": 0})
            total_bucket = totals.setdefault(entity, {"input": 0, "output": 0, "filtered": 0})

            in_c = rec.get("input_count")
            out_c = rec.get("output_count")
            filt_c = rec.get("filtered_count")

            if in_c is not None:
                ent_bucket["input"] += int(in_c)
                total_bucket["input"] += int(in_c)
            if out_c is not None:
                ent_bucket["output"] += int(out_c)
                total_bucket["output"] += int(out_c)
            if filt_c is not None:
                ent_bucket["filtered"] += int(filt_c)
                total_bucket["filtered"] += int(filt_c)

        return {"by_step": by_step, "totals": totals}

    def render_text_summary(self) -> str:
        if not self.records:
            return "No metrics recorded."

        summary = self.build_summary()
        lines: List[str] = []

        lines.append("Per-step metrics:")
        for step in sorted(summary["by_step"].keys()):
            step_bucket = summary["by_step"][step]
            for entity, agg in step_bucket.items():
                lines.append(
                    f"  - {step} [{entity}]: input={agg['input']}, output={agg['output']}, filtered={agg['filtered']}"
                )

        lines.append("")
        lines.append("Totals by entity:")
        for entity in sorted(summary["totals"].keys()):
            agg = summary["totals"][entity]
            lines.append(
                f"  - {entity}: input={agg['input']}, output={agg['output']}, filtered={agg['filtered']}"
            )

        return "\n".join(lines)

    def write(self):
        data = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": self.build_summary(),
            "records": self.records,
        }
        atomic_json(self.output_path, data, indent=2)

    def clear(self):
        self.records.clear()
