from __future__ import annotations

from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from ..db import FeedbackRecord, MetricSlice


def _ratio(numerator: Decimal, denominator: Decimal) -> float | None:
    return float(numerator / denominator) if denominator else None


def _delta(base: float | None, current: float | None) -> float | None:
    return round((current / base - 1) * 100, 4) if base not in (None, 0) and current is not None else None


def _metric_values(row: MetricSlice) -> dict:
    spend = Decimal(str(row.spend))
    revenue = Decimal(str(row.revenue_d7))
    impressions = Decimal(row.impressions)
    clicks = Decimal(row.clicks)
    installs = Decimal(row.installs)
    reach = Decimal(row.reach) if row.reach else Decimal(0)
    return {
        "ctr": _ratio(clicks, impressions),
        "cvr": _ratio(installs, clicks),
        "cpi": _ratio(spend, installs),
        "roas_d7": _ratio(revenue, spend),
        "frequency": _ratio(impressions, reach),
    }


def _anomalous(metrics: dict, baseline: MetricSlice, current: MetricSlice) -> bool:
    ctr = metrics["ctr"]["delta_pct"]
    cvr = metrics["cvr"]["delta_pct"]
    roas = metrics["roas_d7"]["delta_pct"]
    return baseline.impressions >= 10000 and current.impressions >= 10000 and (
        (ctr is not None and ctr <= -20 and cvr is not None and cvr <= -20)
        or (roas is not None and roas <= -30)
    )


def snapshot(session, dataset_id: str) -> dict:
    rows = session.scalars(select(MetricSlice).where(MetricSlice.dataset_id == dataset_id)).all()
    pairs: dict[tuple, dict[str, MetricSlice]] = {}
    for row in rows:
        key = (row.market, row.platform, row.campaign_id, row.creative_id)
        pairs.setdefault(key, {})[row.window_type] = row
    candidates = []
    for key, pair in pairs.items():
        if set(pair) != {"baseline", "current"}:
            continue
        base, current = pair["baseline"], pair["current"]
        values = _metric_values(base), _metric_values(current)
        metrics = {name: {"baseline": values[0][name], "current": values[1][name], "delta_pct": _delta(values[0][name], values[1][name])} for name in values[0]}
        anomaly = _anomalous(metrics, base, current)
        score = sum(max(0, -(metrics[name]["delta_pct"] or 0)) for name in ("ctr", "cvr", "roas_d7"))
        candidates.append({"market": key[0], "platform": key[1], "campaign_id": key[2], "creative_id": key[3], "metrics": metrics, "anomaly": anomaly, "score": score, "source_ids": {"baseline": base.id, "current": current.id}, "raw": {"baseline": {"spend": base.spend, "impressions": base.impressions, "reach": base.reach, "destination_clicks": base.clicks, "installs": base.installs, "revenue_d7": base.revenue_d7}, "current": {"spend": current.spend, "impressions": current.impressions, "reach": current.reach, "destination_clicks": current.clicks, "installs": current.installs, "revenue_d7": current.revenue_d7}}})
    if not candidates:
        raise ValueError("NO_METRIC_PAIRS")
    target = sorted(candidates, key=lambda item: (-int(item["anomaly"]), -item["score"], item["market"], item["creative_id"]))[0]
    controls = [f'{item["market"]} / {item["creative_id"]}' for item in candidates if item is not target and not item["anomaly"]]
    return {"target": target, "stable_controls": controls, "slice_count": len(rows), "formula_version": "metrics-1.0"}


def local_topic(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("repeat", "too often", "many times", "again and again", "frequen", "muitas vezes", "repet", "demasiadas veces", "otra vez", "反复", "重复曝光")):
        return "repeat_exposure"
    if any(word in lowered for word in ("promis", "mismatch", "different from", "not as advertised", "promete", "promessa", "não coincide", "no coincide", "承诺", "不符")):
        return "promise_mismatch"
    return "other"


def feedback_cluster(session, dataset_id: str, target: dict, model_labels: dict[str, str] | None = None) -> dict:
    rows = session.scalars(select(FeedbackRecord).where(FeedbackRecord.dataset_id == dataset_id)).all()
    linked = []
    for row in rows:
        if row.window_type not in {"baseline", "current"} or row.market != target["market"]:
            continue
        direct = row.platform == target["platform"] and row.campaign_id == target["campaign_id"] and row.creative_id == target["creative_id"]
        if direct or row.source == "app_store_review":
            linked.append(row)
    counts = {window: Counter() for window in ("baseline", "current")}
    total = Counter()
    strength = "none"
    for row in linked:
        direct = row.platform == target["platform"] and row.campaign_id == target["campaign_id"] and row.creative_id == target["creative_id"]
        if row.source == "ad_comment" and direct:
            strength = "A"
        elif row.source == "new_user_survey" and direct and row.link_method == "mmp_attribution" and strength != "A":
            strength = "B"
        elif direct and strength not in {"A", "B"}:
            strength = "C"
        elif strength == "none":
            strength = "D"
        topic = (model_labels or {}).get(row.id) or (row.topic if row.topic != "unclassified" else local_topic(row.text))
        counts[row.window_type][topic] += 1
        total[row.window_type] += 1
    related = {window: counts[window]["repeat_exposure"] + counts[window]["promise_mismatch"] for window in counts}
    share = {window: round(related[window] / total[window], 4) if total[window] else 0 for window in counts}
    return {"related": related, "share": share, "repeat_exposure": {w: counts[w]["repeat_exposure"] for w in counts}, "promise_mismatch": {w: counts[w]["promise_mismatch"] for w in counts}, "total": dict(total), "link_strength": strength, "representative_ids": [row.id for row in linked[:8]], "classification_mode": "model_plus_rules" if model_labels else "rules"}


def correlate(snapshot_artifact: dict, feedback_artifact: dict) -> dict:
    target = snapshot_artifact["target"]
    metric_anomaly = target["anomaly"]
    related = feedback_artifact["related"]
    feedback_rise = related["current"] > related["baseline"] and related["current"] >= 2
    strength = feedback_artifact["link_strength"]
    cross = metric_anomaly and feedback_rise and strength in {"A", "B", "C"}
    kind = "cross_domain" if cross else "metric_only" if metric_anomaly else "feedback_only" if feedback_rise else "none"
    hypotheses = []
    if kind == "cross_domain":
        if target["metrics"]["frequency"]["delta_pct"] is not None and target["metrics"]["frequency"]["delta_pct"] > 20 and feedback_artifact["repeat_exposure"]["current"] > feedback_artifact["repeat_exposure"]["baseline"]:
            hypotheses.append("素材疲劳")
        if feedback_artifact["promise_mismatch"]["current"] > feedback_artifact["promise_mismatch"]["baseline"]:
            hypotheses.append("承诺错位")
    return {"kind": kind, "link_strength": strength if cross else "none", "causality": "unverified", "hypotheses": hypotheses, "alternative_explanations": ["竞价环境变化", "受众结构变化", "归因口径偏差"], "metric_anomaly": metric_anomaly, "feedback_rise": feedback_rise}
