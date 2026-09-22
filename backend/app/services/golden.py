from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db import AnalysisWindow, Dataset, FeedbackRecord, MetricSlice


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class MetricFixture:
    market: str
    platform: str
    campaign: str
    creative: str
    baseline: tuple[float, int, int, int, int, float]
    current: tuple[float, int, int, int, int, float]


METRICS = (
    MetricFixture("BR", "Meta", "CMP-BR-01", "CR-BR-17", (1000, 100000, 50000, 2500, 500, 1400), (1100, 120000, 30000, 1800, 216, 550)),
    MetricFixture("BR", "Meta", "CMP-BR-01", "CR-BR-22", (500, 50000, 25000, 1200, 240, 700), (525, 52500, 25000, 1260, 252, 735)),
    MetricFixture("US", "Meta", "CMP-US-01", "CR-US-22", (1000, 100000, 50000, 2400, 480, 1400), (1050, 105000, 50000, 2520, 504, 1470)),
    MetricFixture("MX", "TikTok", "CMP-MX-01", "CR-MX-31", (400, 40000, 20000, 1000, 200, 560), (420, 42000, 20000, 1050, 210, 588)),
)


def _feedback_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    scopes = [
        ("BR", "pt-BR", "Meta", "CMP-BR-01", "CR-BR-17", "baseline", 20, 2, 2),
        ("BR", "pt-BR", "Meta", "CMP-BR-01", "CR-BR-17", "current", 30, 11, 7),
        ("US", "en-US", "Meta", "CMP-US-01", "CR-US-22", "baseline", 10, 1, 1),
        ("US", "en-US", "Meta", "CMP-US-01", "CR-US-22", "current", 11, 1, 1),
        ("MX", "es-MX", "TikTok", "CMP-MX-01", "CR-MX-31", "baseline", 7, 1, 0),
        ("MX", "es-MX", "TikTok", "CMP-MX-01", "CR-MX-31", "current", 8, 1, 0),
    ]
    templates = {
        "pt-BR": {
            "repeat_exposure": "Estou vendo este anúncio muitas vezes e ele já ficou cansativo.",
            "promise_mismatch": "O anúncio promete uma experiência que não encontrei no aplicativo.",
            "other": "Comentário sintético sobre a experiência geral do aplicativo.",
        },
        "en-US": {
            "repeat_exposure": "I have seen this ad repeatedly, but the app experience is otherwise stable.",
            "promise_mismatch": "The ad wording feels slightly different from the in-app experience.",
            "other": "Synthetic feedback about the general app experience.",
        },
        "es-MX": {
            "repeat_exposure": "He visto este anuncio varias veces, aunque la experiencia sigue estable.",
            "promise_mismatch": "La promesa del anuncio no coincide completamente con la experiencia.",
            "other": "Comentario sintético sobre la experiencia general de la aplicación.",
        },
    }
    serial = 0
    for market, locale, platform, campaign, creative, window, total, repeats, mismatches in scopes:
        for index in range(total):
            serial += 1
            if index < repeats:
                topic = "repeat_exposure"
            elif index < repeats + mismatches:
                topic = "promise_mismatch"
            else:
                topic = "other"
            rows.append(
                {
                    "id": f"fb_{serial:03d}",
                    "market": market,
                    "locale": locale,
                    "platform": platform,
                    "campaign_id": campaign,
                    "creative_id": creative,
                    "window_type": window,
                    "source": "ad_comment" if index % 2 == 0 else "attributed_survey",
                    "topic": topic,
                    "text": f"{templates[locale][topic]} [synthetic-{serial:03d}]",
                }
            )
    return rows


def manifest() -> dict:
    return {
        "fixture": "golden-br-cr17-v1",
        "feedback_rows": 86,
        "metric_rows": 8,
        "target": {
            "market": "BR",
            "platform": "Meta",
            "campaign_id": "CMP-BR-01",
            "creative_id": "CR-BR-17",
            "metrics": {
                "ctr": {"baseline": 0.025, "current": 0.015, "delta_pct": -40.0},
                "cvr": {"baseline": 0.20, "current": 0.12, "delta_pct": -40.0},
                "cpi": {"baseline": 2.0, "current": 5.0925925926, "delta_pct": 154.62962963},
                "roas_d7": {"baseline": 1.4, "current": 0.5, "delta_pct": -64.28571429},
                "frequency": {"baseline": 2.0, "current": 4.0, "delta_pct": 100.0},
            },
            "feedback": {
                "related": {"baseline": 4, "current": 18},
                "share": {"baseline": 0.20, "current": 0.60},
                "repeat_exposure": {"baseline": 2, "current": 11},
                "promise_mismatch": {"baseline": 2, "current": 7},
            },
        },
    }


def create_golden_dataset(session: Session, workspace_id: str) -> Dataset:
    payload = json.dumps(manifest(), sort_keys=True, separators=(",", ":")).encode()
    dataset = Dataset(
        id=new_id("ds"),
        workspace_id=workspace_id,
        source="golden",
        status="ready",
        name="Golden Fixture",
        feedback_rows=86,
        metric_rows=8,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    session.add(dataset)
    session.flush()
    session.add_all(
        [
            AnalysisWindow(id=new_id("win"), dataset_id=dataset.id, window_type="baseline", start_date="2026-08-18", end_date="2026-08-24"),
            AnalysisWindow(id=new_id("win"), dataset_id=dataset.id, window_type="current", start_date="2026-08-25", end_date="2026-08-31"),
        ]
    )
    for row in _feedback_rows():
        record = dict(row)
        fixture_id = record.pop("id")
        session.add(FeedbackRecord(id=f"{dataset.id}_{fixture_id}", workspace_id=workspace_id, dataset_id=dataset.id, **record))
    for fixture in METRICS:
        for window, values in (("baseline", fixture.baseline), ("current", fixture.current)):
            spend, impressions, reach, clicks, installs, revenue = values
            session.add(
                MetricSlice(
                    id=new_id("metric"),
                    workspace_id=workspace_id,
                    dataset_id=dataset.id,
                    window_type=window,
                    market=fixture.market,
                    platform=fixture.platform,
                    campaign_id=fixture.campaign,
                    creative_id=fixture.creative,
                    spend=spend,
                    impressions=impressions,
                    reach=reach,
                    clicks=clicks,
                    installs=installs,
                    revenue_d7=revenue,
                    currency="USD",
                )
            )
    return dataset
